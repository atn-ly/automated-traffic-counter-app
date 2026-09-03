
## v0.48.20
- Daily Trends: changed both daily charts from stacked bars to time-series line charts.
- Daily multimodal traffic counts now use one line per traffic class with visible points for each date.
- Daily enter and exit counts use separate Enter and Exit lines, with Enter green and Exit red.
- The exported Daily Trends HTML report now uses the same two line-chart views.
- Weather in the HTML report is shown in a separate responsive card grid above the charts so labels and icons cannot be cut off or overlap the chart axes.

# Build milestones

The order below keeps each release usable while protecting data quality.

## v0.48.16 — Full-screen video with controls

- Makes the bottom **Full screen** button open a true full-screen player instead of leaving the video at its embedded size.
- Keeps the Play, Stop, speed, timeline, time, and Exit full screen controls visible directly below the video while full screen is active.
- Restores the compact embedded player automatically when exiting full screen with the button, Esc, or the window close action.

## v0.48.15 — Stable compact video-player sizing

- Keeps the embedded Preprocessing and Quality Control video canvas compact instead of
  expanding it to the full source aspect-ratio height.
- Preserves the video aspect ratio inside the compact canvas, leaving black space on the
  sides when the page is wider than the video.
- Uses one vertical layout for the video and playback controls so the control bar always
  stays directly against the bottom of the video through minimize, maximize, and full-screen
  resize cycles.

## v0.48.14 — Directly positioned video controls

- Removes the automatic vertical layout from the shared video player.
- Positions the controls from the video's calculated bottom edge on every resize.
- Prevents Windows Qt layout stretching from separating the controls from the video.

## v0.48.13 — Enforced fitted video-player container

- Locks each windowed video-player container to the video plus its control row.
- Prevents Qt from inserting flexible height between the video and controls.
- Adds page stretch only after the complete player in Preprocessing and Quality Control.

## v0.48.12 — Video controls stay with the video

- Places the playback controls immediately below the fitted video in Preprocessing.
- Places the playback controls immediately below the fitted video in Quality Control.
- Keeps unused page space below the controls instead of between the video and controls.

## v0.48.11 — Fitted Preprocessing and Quality Control videos

- Sizes the Preprocessing combined-video player to the loaded recording's aspect ratio.
- Sizes the Quality Control annotated-video player to the loaded recording's aspect ratio.
- Removes the large black areas above and below both videos without cropping or stretching.
- Keeps full-screen playback expandable and restores fitted sizing when full screen closes.

## v0.48.10 — Fitted Review Results snapshots

- Sizes the Review Results evidence viewer to each snapshot's actual aspect ratio.
- Removes the large black areas above and below loaded snapshots without cropping or stretching.
- Keeps the viewer fitted when the window or sidebar width changes.

## v0.48.9 — Refined workflow sidebar

- Adds compact icons and clearer spacing to every workflow destination.
- Removes the empty branch boxes and gives Counting a clean expandable arrow and indented submenu.
- Uses a dark-blue selected state with a green accent and a softer grey panel with shadow.
- Moves the app version to a fixed footer at the bottom of the sidebar.

## v0.48.8 — Cleaner direction controls

- Removes the redundant **Crossing labels Enter / Exit** text beside the direction-swap button.
- Keeps the preview's green Enter and red Exit markers as the visual direction reference.

## v0.48.7 — Blue counting lines everywhere

- Changes saved, active, and in-progress counting lines in Line Setup to blue.
- Uses the same blue for counting lines in review evidence and annotated QC videos.
- Invalidates older green annotated-video caches so regenerated QC videos use the new color.

## v0.48.6 — Clearer Line Setup colors and tool hints

- Changes the active Draw Line control from green to blue.
- Keeps Enter labels green and Exit labels red, even after their sides are swapped.
- Removes redundant zone-mode instructions from the zone status text.
- Replaces the crossing-label-style cursor badge with a dark tool hint and colored dot.

## v0.48.5 — Persistent Line Setup drawing modes

- Adds separate, highlighted **Draw Line** and **Draw Zone** modes.
- Shows the active mode and the next required point beside the cursor over the preview.
- Keeps Zone mode active after a zone is completed, cleared, or refreshed.
- Changes modes only through an explicit mode choice or a line-specific Add/Redraw action.

## v0.48.4 — Verified Line Setup sizing and Daily Trends filename

- Enforces the loaded frame's actual aspect-ratio height inside the nested Line Setup
  splitter instead of relying on a height hint that Qt can ignore.
- Converts the previous default `daily_report.html` export name to `daily_trends.html`.

## v0.48.3 — Consistent Daily Trends exports

- Renames the default exported file to `daily_trends.html`.
- Changes the HTML browser title and main report heading from Daily Report to Daily Trends.
- Keeps source recordings stored inside a project connected when the project folder is
  renamed.
- Automatically repairs recording paths left broken by project renames in v0.48.0–v0.48.2.

## v0.48.2 — Aspect-fitted Line Setup preview

- Makes the Line Setup snapshot height follow the loaded recording's aspect ratio.
- Keeps the complete frame visible without stretching or cropping while removing the large
  black areas above and below wide recordings.

## v0.48.1 — Videos instructions placement

- Moves the short recording-selection instructions directly below the **Videos** heading.

## v0.48.0 — Consistent navigation and complete project renaming

- Aligns the Counting landing-page heading and content margins with every other tab.
- Returns the main content scrollbar to the top whenever a workflow page is selected.
- Changes **Rename selected** so it renames the project folder as well as the displayed name.
- Keeps the moved database, evidence paths, preprocessed videos, annotated QC videos, and
  active-project selection connected to the renamed folder.

## v0.47.0 — Weather pictures and clearer Videos page

- Adds the missing **Videos** section heading above the recording controls.
- Adds a distinct vector picture for every supported weather condition: sunny, partly
  cloudy, cloudy, foggy, drizzle, rain, snow, thunderstorms, and unknown conditions.
- Shows weather pictures in the Daily Trends table and in compact date cards above both
  in-app daily charts.
- Embeds weather pictures beside conditions in the HTML table and above the bars in both
  exported daily charts.

## v0.46.0 — Project naming and clearer workflow text

- Adds **Rename selected** on Home so a project name can change without moving its folder,
  database, videos, or generated files.
- Keeps the Counting introduction in one continuous sentence.
- Shortens the Videos instructions and removes the Videos-table double-click shortcut that
  silently checked a recording on Detection.

## v0.45.0 — Upper-body virtual ground tracking

- Estimates a conservative hidden lower-body position when a pedestrian track begins with
  only a short upper-body box.
- Moves that virtual ground position with the visible torso so the person can cross a line
  below the box even when their legs never become visible.
- Leaves normal full-body pedestrians and every vehicle class on their existing real box
  geometry, and keeps the partial-box transition guard that prevents resize-only crossings.
- Invalidates earlier annotation caches so regenerated QC videos use the updated counts.

## v0.44.0 — Four-corner distant detection zones

- Replaces the two-click axis-aligned distant zone with a four-corner tool for angled and
  perspective-shaped sidewalks.
- Straightens the selected area for enlarged YOLO inference, maps its detections back into
  the original frame, and merges them with full-frame detections before one tracker.
- Keeps earlier two-corner project zones working as rectangles when existing projects open.
- Invalidates earlier annotation caches so regenerated QC videos use the saved four-corner
  geometry.

## v0.43.1 — Cleaner annotation overlays

- Removes the bottom-centre tracking dot from annotated QC videos and review snapshots.
- Keeps the same lower-body and bottom-centre crossing calculations internally, so removing
  the visual marker does not change any counts.
- Invalidates the previous annotation cache so regenerated videos use the cleaner overlay.

## v0.43.0 — Optional distant-object detection zone

- Adds a user-drawn distant detection rectangle in Line Setup and stores it with each
  recording.
- Enlarges only that crop for a second YOLO pass, improving recall when distant pedestrians
  are too small to receive a full-frame box.
- Makes the crop authoritative inside its rectangle and merges both passes before one
  ByteTrack instance, preventing duplicate full-frame/crop tracks.
- Applies the zone to combined-video source recordings and alongside lines copied to the
  same camera and date.
- Uses the same zone-aware path when Final QC must regenerate detections.
- Changes the Daily Trends heading to “Daily enter and exit counts.”

## v0.37.0 — Partial-person tracking stability

- Detects abrupt changes between full-body, upper-body, and lower-body pedestrian boxes.
- Maintains a continuous virtual counting anchor through those changes, preventing the box
  resize or fragment switch itself from becoming a false crossing.
- Continues following the visible body fragment after stabilization so a real subsequent
  crossing can still be counted.
- Replaces the deprecated repeated `half` inference argument with FP16 `quantize=16` on
  CUDA, eliminating the command-window warning without disabling the NVIDIA GPU.
- Invalidates old annotated-video caches so Final QC reflects the corrected crossings.

## v0.36.1 — Lower-body crossing consensus

- Requires agreement from two horizontal parts of a detected person's lower body before
  recording a crossing, while allowing that support to arrive over adjacent sampled frames.
- Rejects tied Enter/Exit evidence and ignores one-corner box jitter near angled lines.
- Widens the line deadband slightly so tiny detector-box changes do not become crossings.

## v0.36.0 — Audited crowded-sidewalk crossing recovery

- Reprocessed the uploaded 13-minute Camera 1 audit clip against its saved diagonal line.
- Replaced the single-point crossing decision with a compact lower-body zone so a clipped
  or briefly occluded track can cross even when its foot point first appears past the line.
- Kept one event per track and line, finite-segment endpoint checks, and the visible
  bottom-centre ground-reference dot.
- Bumped the annotated-video cache so Final QC cannot silently reuse pre-fix output.
- On the reproducible YOLO26s/960/stride-3 audit pass, improved the clip from 57 Enter and
  45 Exit to 65 Enter and 55 Exit against the manual 62 Enter and 55 Exit reference.

## v0.35.2 — Restore validated crossing anchor

- Reverted the true-centre experiment after the audited clip regressed from 67 Enter and
  37 Exit to 49 Enter and 36 Exit.
- Restored the bottom-centre ground point used by the previous, better-performing build.
- Kept the Windows annotated-video lock fix and the cleaned Detection-screen wording.
- Left the remaining Exit undercount uncalibrated: it requires clip-level diagnosis rather
  than a hardcoded adjustment to reported totals.

## v0.35.1 — Windows QC video lock hotfix

- Releases the in-app QC player before removing a stale annotated video.
- Continues processing when Windows needs extra time to release the MP4 instead of printing
  repeated PermissionError tracebacks.
- Bumped the final annotated-video cache filename to v7 so an older locked v6 file cannot
  block the corrected build.
- Added regression coverage for a locked annotated QC file.

## v0.35.0 — Directional crossing correction

- Changed line crossing from the bottom edge of the detection box to its true centre, matching
  the tracking dot originally requested for line auditing.
- Registers a crossing earlier when a person is moving toward an occluded or weakly detected
  side of the line, reducing direction-specific missed counts.
- Updated review evidence and annotated QC dots to show the exact centre used by counting.
- Removed version-history wording from the Run object detection description.
- Added regression coverage for a valid centre crossing that the old box-bottom anchor missed.

## v0.34.0 — Streamlined and practical detection

- Replaced separate model, stride, resolution, and batch controls with one Processing mode
  selector: Fast, Recommended, or Maximum accuracy.
- Changed the normal default from YOLO26x at 1280 to YOLO26s at 960 with frame stride 3;
  retained the version 33 workload only as the clearly labeled very-slow option.
- Kept a CPU/laptop preset using YOLO26n at 640 and automatic batching.
- Explicitly selects CUDA when available, enables FP16 inference on NVIDIA GPUs, and shows
  CUDA or CPU in the live progress message.
- Preserved the tracking, angled-line, review, QC, and reporting accuracy fixes from
  version 33 without exposing their internal tuning as extra UI settings.

## v0.33.0 — Detection accuracy and tracking stability

- Changed the Detection defaults to YOLO26x, frame stride 3, and 1280-pixel inference so
  small and distant road users retain substantially more image detail.
- Added selectable 640, 960, 1280, and 1600 inference resolutions and stored the selected
  resolution with every processed recording.
- Made Auto batch account for model scale, resolution, and available CUDA memory; YOLO26x
  at 1280 uses a conservative batch on 16 GB GPUs instead of the old batch of 16.
- Added a traffic-tuned ByteTrack configuration with a longer track buffer for brief
  occlusions and crowded sidewalks.
- Added a five-percent endpoint tolerance for angled counting lines while continuing to
  reject crossings on distant invisible line extensions.
- Added regression coverage for high-resolution detector arguments, safe CUDA batching,
  angled-line endpoints, and persisted processing resolution.

## v0.32.0 — Daily weather context

- Added project-level historical weather loading with an editable location and Edmonton as
  the default.
- Cached daily high, low, WMO condition, and precipitation values inside each project.
- Added weather columns to the Daily Report table and weather context to both in-app charts.
- Added high/low and condition labels above each daily bar in the HTML report, with
  Open-Meteo attribution.

## v0.31.1 — Daily Report section headings

- Added visible headings above the counts-by-day table and both daily plots.

## v0.31.0 — Multi-date Daily Report

- Added **8 Daily Report** after Camera Comparison.
- Added a sortable counts-by-day table with every supported class, Enter, Exit, and totals.
- Added daily multimodal and Enter/Exit stacked plots with recording dates on the horizontal
  axis and counts on the vertical axis.
- Added a self-contained Daily Report HTML export containing the table and both plots.

## v0.30.0 — Report terminology and hourly camera bars

- Standardized Camera Reports and Camera Comparison on the word **counts**.
- Renamed the camera HTML output and its main totals section to **Camera report** and
  **Multimodal traffic counts**.
- Added a stacked bar chart beside the hourly camera comparison line chart, both in-app and
  in the all-camera HTML report.

## v0.29.0 — One-time Default Project migration

- Added a Home action that appears only while the legacy Default Project database exists.
- Moves the legacy evidence, preprocessed, and Final QC folders into a named project folder.
- Creates a verified SQLite backup before removing the legacy database and rewrites stored
  evidence-frame paths to the new project location.
- Automatically opens the migrated project and removes the obsolete Default Project entry.
- Defers deletion of a Windows-locked legacy database until a later app launch instead of
  rolling back a completed migration.

## Milestone 0 — Runnable workflow shell (included)

**Outcome:** A local desktop project can load videos, save a per-video line, run a
background detector, review events, and export accepted counts.

Acceptance checks:

- the interface stays responsive while a long video is processed;
- frames are streamed and not accumulated in memory;
- closing or cancelling processing stops safely;
- a video cannot be processed until its line is saved;
- clean exports contain accepted events only;
- automated tests cover crossing direction and export filtering.

## Milestone 1 — Recording-time intake and coverage QC (included)

**Outcome:** A folder of recordings becomes a trustworthy processing queue.

Included:

- date and 24-hour start time read from the `YYYYMMDD_HHMMSS` filename prefix;
- seconds `58` and `59` rounded forward to the next minute;
- responsive per-file metadata, filename-time, and save progress during video intake;
- editable camera/location assignment;
- grouping by camera and recording date;
- a Preprocessing screen with a sortable source list, visual recorded-versus-gap
  timeline, and exact gap details across configurable expected hours;
- fast camera-day MP4 assembly in timestamp order, stream-copying compatible H.264/HEVC
  recordings even when their recorded frame rates differ, encoding only gap cards, and
  estimating remaining work from copied bytes; gaps of three seconds or less receive no card;
- full-screen combined-video playback, selectable 0.5× to 4× speed, cancellation,
  elapsed/remaining time, stale-output detection, and save-copy export;
- hourly recorded, missing, and overlapping minutes;
- counts placed into exact hourly bins even when fragments cross an hour boundary;
- plots, coverage QC CSV, and a daily HTML report.

Still to add:

- duplicate-content fingerprinting and moved-file reconciliation;
- batch camera naming and batch manual time editing;
- configurable expected operating hours rather than always displaying all 24 hours.

Acceptance check: a user can explain every missing hour before detection begins.

## Milestone 2 — Reliable line setup for changing camera angles

**Outcome:** Each recording day can use one or more correct counting geometries.

Included now:

- choose a recording from a list inside Line Setup and load its preview without returning
  to Videos;
- add, name, select, redraw, and delete multiple lines on one recording;
- show the selected line in green and other saved lines in blue;
- copy the complete line set to the same camera and date;
- swap the Enter/Exit mapping independently for each line;
- attribute review evidence and exported events to the line that produced them.

Build:

- scrub to a representative frame instead of using only the first frame;
- line labels such as `toward Whyte Ave` and `away from Whyte Ave`;
- polygon exclusion zones;
- copy yesterday's line as a starting point, then adjust it;
- warn when the saved line is outside a changed image resolution.

Acceptance check: lines can be audited and reproduced for every processed video.

## Milestone 3 — Validated multimodal detection and tracking

**Outcome:** Pedestrians, bicycles, cars, motorcycles, buses, and trucks are counted with
known performance.

Build:

- benchmark YOLO model sizes and ByteTrack/BoT-SORT settings on labelled OSBA clips;
- define and validate the final traffic class mapping;
- finite-segment crossing so disjoint sidewalks do not trigger another line's extension;
- lower-body crossing zones for horizontal, vertical, and angled lines, including tolerance
  for sampled points that land directly on a line and tracks clipped near the line;
- region-of-interest filtering, minimum track age, and crossing hysteresis;
- GPU detection with a CPU fallback;
- resumable chunk checkpoints for multi-hour recordings;
- record run settings, model checksum, processing speed, and failures.

Acceptance check: report precision, recall, and count error by mode and camera condition.

## Milestone 4 — Fast visual review and correction

**Outcome:** A reviewer can correct questionable events without watching every hour.

Included now:

- single-frame boxed evidence for every counted crossing;
- sortable recording and detection-result tables whose selections remain tied to stable IDs;
- an Annotated Video quality-control screen that builds a full-date MP4 using the saved
  detection model, classes, and frame stride;
- an optional, default-on detection-time annotation cache that writes boxes and counting
  lines during the existing YOLO pass, then assembles Final QC without repeating inference;
- bounded multi-frame YOLO inference with Auto selecting 16 frames on CUDA GPUs with at
  least 12 GB of memory, 8 on smaller CUDA GPUs, and 1 on CPU, with overlapping frame
  decoding and result writing while the persistent tracker consumes results in timestamp
  order;
- visible recording-gap cards, in-app playback, cancellation, and save-copy export.
- boxed line-name and Enter/Exit labels that remain readable in the final annotated video;
- mixed-frame-rate assembly that preserves playback duration, reports assembly progress,
  and overlays running detected-crossing totals by selected class;
- full-screen annotated-video playback and selectable 0.5× to 4× playback speed;
- a header control that hides or restores the left workflow menu, which is shown at launch;

Included now:

- save a boxed evidence frame at every counted crossing;
- select, accept, reject, and delete detection events;
- filter Review with Pending, Accepted, Rejected, and All sub-tabs;
- use checkboxes for clear bulk review actions;
- keep video management separate from the authoritative Detection processing checklist;
- expose Preprocessing, Line Setup, Counting, Quality Control, Reports, and Camera
  Comparison in a persistent left-side menu;
- allow Line Setup to preview a combined camera-day video and apply its lines to every
  represented source fragment;
- allow Detection to select combined camera-day rows while processing original frames so
  event timestamps remain exact and gap cards are ignored;
- allow highlighted combined camera-day outputs to be deleted from Detection without
  removing their original recordings, lines, detections, or review decisions;
- bulk-accept pending events at or above a chosen confidence threshold;
- remove evidence automatically when its detection or video is removed.

Next build:

- save a short clip around each crossing;
- add retention controls for large annotation caches;
- prioritize low-confidence, occluded, and near-duplicate events;
- keyboard shortcuts for accept, reject, reclassify, previous, and next;
- edit mode and direction; add missed events manually;
- audit trail with original value, corrected value, reviewer, and timestamp.

Acceptance check: two reviewers can independently reproduce the clean totals.

## Milestone 5 — Analysis, exports, and reporting

**Outcome:** Clean counts become analysis-ready OSBA deliverables.

Included now:

- camera/date hourly stacked plots inside Camera Reports;
- a separate Camera Comparison screen whose first/default view is an Enter/Exit table,
  followed by total-count and per-recorded-hour chart metrics;
- a top-aligned Camera Comparison layout matching Camera Reports, with direct clean-CSV and
  all-camera HTML report exports;
- hourly comparison line and stacked bar charts, plus a scrollable multi-panel figure that
  repeats each camera's hourly stacked class plot;
- separate HTML scopes: Camera Reports exports one camera's multimodal traffic counts,
  direction summary,
  hourly table, and hourly plot, while Camera Comparison owns all-camera figures;
- selected-date stacked count comparisons across every camera, in-app and in HTML;
- an in-app camera summary table with accepted Enter, Exit, per-camera, and overall totals;
- in-app report views for daily class totals and 24 hourly bins;
- a separate multi-date Daily Report table with daily multimodal and Enter/Exit plots;
- event-level, tidy hourly-count, and coverage-QC CSV exports;
- accepted daily totals by mode and direction;
- self-contained HTML report with a stacked hourly class plot, coverage, and methods notes.

Next build:

- weekday/weekend, location, and event summaries;
- event-versus-regular-day comparisons;
- missing-data completeness notes alongside every total;
- charts and an editable Excel workbook;
- reproducible PDF summary with methods and model/version metadata.

Acceptance check: every chart traces back to accepted event rows and source video.

## Milestone 6 — Packaging and field deployment

**Outcome:** Staff can install and operate the app without a developer.

Included now:

- a Home project hub for creating and switching between event projects such as Art Walk
  and Fringe Festival;
- a welcoming Home introduction and a visual Counting landing page with direct Detection
  and Review Results cards;
- isolated per-project folders containing the database and every generated evidence,
  Preprocessing, QC, and settings artifact;
- last-opened-project persistence and automatic compatibility with the existing database
  as Default Project;
- guarded project switching that is unavailable while loading, detecting, preprocessing,
  or generating annotated video;

Build:

- signed Windows installer and automatic project backups;
- hardware benchmark and recommended processing presets;
- clear recovery from interrupted jobs, missing videos, and full disks;
- privacy retention settings for source video and review evidence;
- user guide plus a small validation dataset.

Acceptance check: a new staff member completes the workflow from videos to report using
the guide alone.
## v0.48.17
- Removed the unused blank area below the compact video player on Preprocessing and Quality Control.
- Spare vertical space is now used by the recordings/results tables instead, while the video remains compact with its controls directly underneath.

## v0.48.18
- Tightened the spacing under the Daily Trends title so its description aligns like the other tabs.
- Kept the Daily Trends description and export actions on the same row, but top-aligned the description instead of vertically centering it beside the taller buttons.

