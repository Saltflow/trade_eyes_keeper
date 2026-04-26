from pathlib import Path
import re

dir_p = Path('data/email_archive')
files = sorted(dir_p.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
f = files[0]
html = f.read_text(encoding='utf-8')
print(f'File: {f.name} ({len(html)} bytes)')

issues = []

# 1. Escaped HTML entities
if '&lt;h3' in html or '&lt;h4' in html or '&lt;table' in html:
    issues.append('ESCAPED HTML TAGS PRESENT')
else:
    print('PASS: No escaped HTML tags')

# 2. Chart CIDs
for cid in ['chart001', 'chart002', 'chart003']:
    if f'cid:{cid}' not in html:
        issues.append(f'MISSING {cid}')

# 3. Portfolio section
sections = ['投资组合预期回报', 'A股投资组合', '非A股投资组合',
            '最高收益', '最小回撤', '最优夏普', '策略说明']
for s in sections:
    if s not in html:
        issues.append(f'MISSING section: {s}')

# 4. Template completeness
html_count = html.count('DOCTYPE html')
if html_count != 2:
    issues.append(f'DOCTYPE count={html_count}, expected 2')

# 5. Check for newlines in unexpected places (broken tags)
broken = re.findall(r'<\s*/\s*h\s*[34]', html)
if broken:
    issues.append(f'Broken tags: {broken}')

if not issues:
    print('=== ALL 10 CHECKS PASSED ===')
else:
    print(f'=== {len(issues)} ISSUES ===')
    for i in issues:
        print(f'  - {i}')
