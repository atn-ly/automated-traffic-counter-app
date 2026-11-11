import re,html,glob,csv,sys
root=sys.argv[1]; tot=[]; hr=[]
for f in sorted(glob.glob(root+'/camera_comparison_*.html')):
    d=re.search(r'(\d{4}-\d\d-\d\d)',f).group(1)
    raw=open(f,encoding='utf-8').read()
    t=re.sub(r'<script.*?</script>|<style.*?</style>','',raw,flags=re.S)
    t=re.sub(r'<(td|th)[^>]*>',' | ',t); t=re.sub(r'</tr>','\n',t)
    t=html.unescape(re.sub(r'<[^>]+>','',t))
    for c,e,x,n,h in re.findall(r'\|\s*Camera_(\d+)\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|\s*([\d.]+)',t):
        tot.append(dict(date=d,camera=int(c),enter=int(e.replace(',','')),exit=int(x.replace(',','')),total=int(n.replace(',','')),hours=float(h)))
    seg=t[t.find('Hourly counts by camera — line chart'):t.find('Stacked bar chart')]
    for c,h,n in re.findall(r'Camera_(\d+) · (\d\d):00 · (\d+)',seg):
        hr.append(dict(date=d,camera=int(c),hour=int(h),count=int(n)))
for name,rows in (('camday.csv',tot),('camhour.csv',hr)):
    w=csv.DictWriter(open(name,'w',newline=''),fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
print(len(tot),len(hr))
