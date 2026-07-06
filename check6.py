import glob, os, re
files = sorted(glob.glob('/root/trade_eyes_keeper/data/email_archive/*.html'), key=os.path.getmtime)
f = files[-1]
html = open(f, encoding='utf-8').read()

print('=== TOP1 HEADER (should have NO test_return/drawdown/sharpe) ===')
print(f'  Top1 策略: {html.count("Top1 策略")}')
print(f'  测试收益: {html.count("测试收益")}')
print(f'  position_target 模式: {html.count("position_target 模式")}')

print('\n=== STRATEGY ALERTS (should be present) ===')
print(f'  策略报警: {html.count("策略报警")}')
print(f'  策略信号: {html.count("策略信号")}')

print('\n=== METRICS (should have PortfolioEvaluator numbers) ===')
for m in re.finditer(r'收益\s*<span[^>]*>([+-]?[\d.]+%)</span>', html):
    print(f'  收益: {m.group(1)}')
for m in re.finditer(r'交易\s*(\d+)笔', html):
    print(f'  交易: {m.group(1)}笔')

print(f'\n=== SIZE ===')
print(f'  {len(html)} chars, File: {os.path.basename(f)}')
