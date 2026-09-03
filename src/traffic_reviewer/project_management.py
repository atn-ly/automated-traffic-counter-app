from __future__ import annotations

import json
import re
import shutil
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROJECT_DATABASE_NAME = "traffic_project_v2.sqlite3"
PROJECT_MANIFEST_NAME = "project.json"
ACTIVE_PROJECT_NAME = "active_project.json"
LEGACY_MIGRATION_NAME = "legacy_project_migration.json"
GENERATED_PROJECT_DIRECTORIES = ("evidence", "final_qc", "preprocessed")


@dataclass(frozen=True)
class ProjectInfo:
    name: str
    directory: Path
    database_path: Path
    is_legacy: bool = False


def project_folder_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name.strip())
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")
    return slug or "project"


class ProjectCatalog:
    """Discover and create isolated OSBA project folders under one data root."""

    def __init__(
        self,
        data_root: Path,
        legacy_database_path: Path | None = None,
    ):
        self.data_root = Path(data_root)
        self.projects_root = self.data_root / "projects"
        self.legacy_database_path = Path(
            legacy_database_path or self.data_root / PROJECT_DATABASE_NAME
        )
        self.state_path = self.data_root / ACTIVE_PROJECT_NAME
        self.migration_state_path = self.data_root / LEGACY_MIGRATION_NAME

    def list_projects(self) -> list[ProjectInfo]:
        project_directories: list[Path] = []
        if self.projects_root.is_dir():
            project_directories = sorted(
                (path for path in self.projects_root.iterdir() if path.is_dir()),
                key=lambda path: path.name.casefold(),
            )

        projects: list[ProjectInfo] = []
        if self._legacy_project_exists() or not project_directories:
            projects.append(
                ProjectInfo(
                    name=self._manifest_name(
                        self.legacy_database_path.parent / PROJECT_MANIFEST_NAME,
                        "Default Project",
                    ),
                    directory=self.legacy_database_path.parent,
                    database_path=self.legacy_database_path,
                    is_legacy=True,
                )
            )

        for directory in project_directories:
            database_path = directory / PROJECT_DATABASE_NAME
            manifest_path = directory / PROJECT_MANIFEST_NAME
            projects.append(
                ProjectInfo(
                    name=self._manifest_name(
                        manifest_path,
                        directory.name.replace("-", " ").title(),
                    ),
                    directory=directory,
                    database_path=database_path,
                )
            )
        return projects

    def active_project(self) -> ProjectInfo:
        projects = self.list_projects()
        relative_path = self._read_json(self.state_path).get("database_path")
        if isinstance(relative_path, str):
            for project in projects:
                try:
                    project_relative = project.database_path.relative_to(self.data_root)
                except ValueError:
                    continue
                if project_relative.as_posix() == relative_path:
                    return project
        return projects[0]

    def project_for_database(self, database_path: Path) -> ProjectInfo:
        resolved = Path(database_path).resolve()
        for project in self.list_projects():
            if project.database_path.resolve() == resolved:
                return project
        return ProjectInfo(
            name=Path(database_path).parent.name or "Project",
            directory=Path(database_path).parent,
            database_path=Path(database_path),
            is_legacy=True,
        )

    def create_project(self, name: str) -> ProjectInfo:
        clean_name = " ".join(name.split())
        if not clean_name:
            raise ValueError("Enter a project name.")
        self.projects_root.mkdir(parents=True, exist_ok=True)
        base_slug = project_folder_name(clean_name)
        directory = self.projects_root / base_slug
        suffix = 2
        while directory.exists():
            directory = self.projects_root / f"{base_slug}-{suffix}"
            suffix += 1
        directory.mkdir()
        self._write_json(
            directory / PROJECT_MANIFEST_NAME,
            {
                "name": clean_name,
                "created_at": datetime.now().astimezone().isoformat(),
            },
        )
        return ProjectInfo(
            name=clean_name,
            directory=directory,
            database_path=directory / PROJECT_DATABASE_NAME,
        )

    def rename_project(self, project: ProjectInfo, name: str) -> ProjectInfo:
        """Rename a project and its folder while preserving all project contents."""
        clean_name = " ".join(name.split())
        if not clean_name:
            raise ValueError("Enter a project name.")
        known_projects = self.list_projects()
        matching_project = next(
            (
                item
                for item in known_projects
                if item.database_path.resolve() == project.database_path.resolve()
            ),
            None,
        )
        if matching_project is None:
            raise ValueError("The selected project is not in the project catalog.")
        if matching_project.is_legacy:
            raise ValueError(
                "Move the Default Project into the Projects folder before renaming it."
            )
        if any(
            item.database_path.resolve() != project.database_path.resolve()
            and item.name.casefold() == clean_name.casefold()
            for item in known_projects
        ):
            raise ValueError(f'A project named "{clean_name}" already exists.')

        old_directory = matching_project.directory
        target_directory = self.projects_root / project_folder_name(clean_name)
        same_directory = old_directory.resolve() == target_directory.resolve()
        if target_directory.exists():
            try:
                same_directory = target_directory.samefile(old_directory)
            except OSError:
                same_directory = False
        if not same_directory and target_directory.exists():
            raise ValueError(
                f'The project folder "{target_directory.name}" already exists.'
            )

        old_database_path = matching_project.database_path
        old_manifest_path = old_directory / PROJECT_MANIFEST_NAME
        old_manifest = self._read_json(old_manifest_path)
        try:
            old_relative_database = old_database_path.relative_to(self.data_root).as_posix()
        except ValueError:
            old_relative_database = ""
        was_active = (
            self._read_json(self.state_path).get("database_path") == old_relative_database
        )
        moved = False
        try:
            if not same_directory:
                old_directory.rename(target_directory)
                moved = True
            new_directory = target_directory if moved else old_directory
            new_database_path = new_directory / PROJECT_DATABASE_NAME
            if moved and new_database_path.is_file():
                self._rewrite_evidence_paths(
                    new_database_path,
                    old_directory,
                    new_directory,
                )
                self._rewrite_project_video_paths(
                    new_database_path,
                    old_directory,
                    new_directory,
                )
            manifest = dict(old_manifest)
            manifest["name"] = clean_name
            manifest.setdefault("created_at", datetime.now().astimezone().isoformat())
            manifest["renamed_at"] = datetime.now().astimezone().isoformat()
            self._write_json(new_directory / PROJECT_MANIFEST_NAME, manifest)
            renamed = ProjectInfo(
                name=clean_name,
                directory=new_directory,
                database_path=new_database_path,
            )
            if was_active:
                self.set_active(renamed)
        except Exception as exc:
            rollback_errors: list[str] = []
            if moved:
                try:
                    if target_directory.exists() and not old_directory.exists():
                        target_directory.rename(old_directory)
                    restored_database_path = old_directory / PROJECT_DATABASE_NAME
                    if restored_database_path.is_file():
                        self._rewrite_evidence_paths(
                            restored_database_path,
                            target_directory,
                            old_directory,
                        )
                        self._rewrite_project_video_paths(
                            restored_database_path,
                            target_directory,
                            old_directory,
                        )
                except (OSError, sqlite3.Error) as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            try:
                if old_manifest:
                    self._write_json(old_manifest_path, old_manifest)
                else:
                    old_manifest_path.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
            detail = f"Project was not renamed: {exc}"
            if rollback_errors:
                detail += " Rollback also reported: " + "; ".join(rollback_errors)
            raise OSError(detail) from exc

        return renamed

    def repair_project_video_paths(self, project: ProjectInfo) -> int:
        """Repair paths left behind by an older project-folder rename."""
        if project.is_legacy or not project.database_path.is_file():
            return 0
        return self._rewrite_project_video_paths(
            project.database_path,
            project.directory,
            project.directory,
            missing_only=True,
        )

    def can_move_legacy_project(self) -> bool:
        return (
            self.legacy_database_path.is_file()
            and not self._legacy_migration_destination_exists()
        )

    def legacy_cleanup_pending(self) -> bool:
        return (
            self._legacy_migration_destination_exists()
            and self.legacy_database_path.exists()
        )

    def move_legacy_project(self, name: str) -> ProjectInfo:
        """Move the pre-project-layout database and generated files into a project."""
        if not self.legacy_database_path.is_file():
            raise ValueError("The Default Project database has already been moved.")

        destination = self.create_project(name)
        old_root = self.legacy_database_path.parent
        moved_directories: list[tuple[Path, Path]] = []
        active_changed = False
        try:
            self._backup_database(
                self.legacy_database_path,
                destination.database_path,
            )
            self._rewrite_evidence_paths(
                destination.database_path,
                old_root,
                destination.directory,
            )
            for directory_name in GENERATED_PROJECT_DIRECTORIES:
                source = old_root / directory_name
                target = destination.directory / directory_name
                if not source.exists():
                    continue
                if target.exists():
                    raise FileExistsError(f"The migration target already contains {target.name}.")
                shutil.move(str(source), str(target))
                moved_directories.append((source, target))

            self.set_active(destination)
            active_changed = True
            self._write_json(
                self.migration_state_path,
                {
                    "database_path": destination.database_path.relative_to(
                        self.data_root
                    ).as_posix(),
                    "migrated_at": datetime.now().astimezone().isoformat(),
                },
            )
        except Exception as exc:
            rollback_errors: list[str] = []
            if active_changed and self.legacy_database_path.exists():
                try:
                    self._write_json(
                        self.state_path,
                        {
                            "database_path": self.legacy_database_path.relative_to(
                                self.data_root
                            ).as_posix()
                        },
                    )
                except (OSError, ValueError) as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            for source, target in reversed(moved_directories):
                try:
                    if target.exists() and not source.exists():
                        shutil.move(str(target), str(source))
                except OSError as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if not rollback_errors:
                shutil.rmtree(destination.directory, ignore_errors=True)
            detail = f"Default Project was not moved: {exc}"
            if rollback_errors:
                detail += " Rollback also reported: " + "; ".join(rollback_errors)
            raise OSError(detail) from exc

        self.cleanup_migrated_legacy_database()
        return destination

    def cleanup_migrated_legacy_database(self) -> bool:
        """Remove the old database once Windows releases it after migration."""
        if not self._legacy_migration_destination_exists():
            return False
        cleanup_paths = [
            self.legacy_database_path,
            Path(str(self.legacy_database_path) + "-wal"),
            Path(str(self.legacy_database_path) + "-shm"),
        ]
        for path in cleanup_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                return False
        try:
            self.migration_state_path.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def set_active(self, project: ProjectInfo) -> None:
        try:
            relative_path = project.database_path.relative_to(self.data_root)
        except ValueError as exc:
            raise ValueError("The selected project is outside the OSBA data folder.") from exc
        known_paths = {
            item.database_path.resolve() for item in self.list_projects()
        }
        if project.database_path.resolve() not in known_paths:
            raise ValueError("The selected project is not in the project catalog.")
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._write_json(
            self.state_path,
            {"database_path": relative_path.as_posix()},
        )

    def _legacy_project_exists(self) -> bool:
        if self._legacy_migration_destination_exists():
            return False
        old_root = self.legacy_database_path.parent
        return self.legacy_database_path.exists() or any(
            (old_root / directory_name).exists()
            for directory_name in GENERATED_PROJECT_DIRECTORIES
        )

    def _legacy_migration_destination_exists(self) -> bool:
        relative_path = self._read_json(self.migration_state_path).get("database_path")
        if not isinstance(relative_path, str) or not relative_path:
            return False
        destination = (self.data_root / relative_path).resolve()
        try:
            destination.relative_to(self.data_root.resolve())
        except ValueError:
            return False
        return destination.is_file()

    @staticmethod
    def _backup_database(source_path: Path, destination_path: Path) -> None:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(source_path)) as source:
            checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and checkpoint[0] != 0:
                raise OSError("The database is busy. Close the app on every computer and retry.")
            with closing(sqlite3.connect(destination_path)) as destination:
                source.backup(destination)
                integrity = destination.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise OSError("The copied project database did not pass its integrity check.")

    @staticmethod
    def _rewrite_evidence_paths(
        database_path: Path,
        old_project_root: Path,
        new_project_root: Path,
    ) -> None:
        old_evidence_root = old_project_root / "evidence"
        new_evidence_root = new_project_root / "evidence"
        with closing(sqlite3.connect(database_path)) as connection:
            rows = connection.execute(
                """
                SELECT id, evidence_path
                FROM count_events
                WHERE evidence_path IS NOT NULL AND evidence_path <> ''
                """
            ).fetchall()
            replacements: list[tuple[str, int]] = []
            for event_id, raw_path in rows:
                path = Path(raw_path)
                try:
                    relative_path = path.relative_to(old_evidence_root)
                except ValueError:
                    evidence_indices = [
                        index
                        for index, part in enumerate(path.parts)
                        if part.casefold() == "evidence"
                    ]
                    if not evidence_indices:
                        continue
                    relative_path = Path(*path.parts[evidence_indices[-1] + 1 :])
                replacements.append(
                    (str(new_evidence_root / relative_path), int(event_id))
                )
            connection.executemany(
                "UPDATE count_events SET evidence_path = ? WHERE id = ?",
                replacements,
            )
            connection.commit()

    @staticmethod
    def _rewrite_project_video_paths(
        database_path: Path,
        old_project_root: Path,
        new_project_root: Path,
        *,
        missing_only: bool = False,
    ) -> int:
        """Move database video paths that refer to files stored inside a project folder."""
        with closing(sqlite3.connect(database_path)) as connection:
            rows = connection.execute("SELECT id, path FROM videos").fetchall()
            replacements: list[tuple[str, int]] = []
            for video_id, raw_path in rows:
                path = Path(raw_path)
                if missing_only and path.exists():
                    continue
                relative_path: Path | None = None
                try:
                    relative_path = path.relative_to(old_project_root)
                except ValueError:
                    project_indices = [
                        index
                        for index, part in enumerate(path.parts[:-1])
                        if part.casefold() == "projects" and index + 2 < len(path.parts)
                    ]
                    if project_indices:
                        relative_path = Path(*path.parts[project_indices[-1] + 2 :])
                if relative_path is None:
                    continue
                candidate = new_project_root / relative_path
                if candidate.is_file():
                    replacements.append((str(candidate), int(video_id)))
            connection.executemany(
                "UPDATE videos SET path = ? WHERE id = ?",
                replacements,
            )
            connection.commit()
        return len(replacements)

    @staticmethod
    def _manifest_name(path: Path, fallback: str) -> str:
        name = ProjectCatalog._read_json(path).get("name")
        return str(name).strip() if isinstance(name, str) and name.strip() else fallback

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(value, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary_path.replace(path)
