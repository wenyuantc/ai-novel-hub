"""在已打开原型页面的 Playwright 会话中验证 Python 码点与真实 Chromium 文档投影。"""
import json
import random
import subprocess

PW = '/Users/wenyuan/.codex/skills/playwright/scripts/playwright_cli.sh'
rng = random.Random(20261108)
alphabet = ['甲', '乙', '😀', '𠀀', 'e', '\u0301', ' ', '\t', '\n', '\r', '\r\n', '*']
examples = ['', '\r\n', '\n\n', '甲😀\r\n\n乙e\u0301\t  \r第三行  \n']
examples.extend(''.join(rng.choices(alphabet, k=rng.randrange(1, 15))) for _ in range(80))
expression = '''() => {
 const cases = PAYLOAD
 return JSON.stringify(cases.map((value) => {
  window.poc.load(value)
  const entries = window.poc.cursors()
  const offsets = Array.from({length: Array.from(value).length + 1}, (_, i) => ({i, candidates:window.poc.toPm(i)}))
  const roundtrip = entries.every(x => window.poc.toCp(x.pm) === x.offset && window.poc.toPm(x.offset).includes(x.pm))
  const inserts = []
  for (const entry of entries.slice(0, 5)) {
   window.poc.load(value)
   window.poc.editor.commands.setTextSelection(entry.pm)
   window.poc.editor.commands.insertContent('新')
   inserts.push({index:entry.offset, result:window.poc.state().text})
  }
  return {value, stored:window.poc.load(value), cpLength:Array.from(value).length, offsets, roundtrip, inserts}
 }))
}'''.replace('PAYLOAD', json.dumps(examples, ensure_ascii=True))
result = subprocess.run([PW, '-s=richproof', '--raw', 'eval', expression], check=True, text=True, capture_output=True)
rows = json.loads(json.loads(result.stdout))
errors=[]
for row in rows:
    original=row['value']
    if row['stored'] != original or row['cpLength'] != len(original) or not row['roundtrip']:
        errors.append(('roundtrip', original, row))
    for item in row['offsets']:
        offset=item['i']
        crlf_inside=0 < offset < len(original) and original[offset-1:offset+1] == '\r\n'
        if bool(item['candidates']) == crlf_inside:
            errors.append(('boundary', original, item))
    for item in row['inserts']:
        i=item['index']
        if item['result'] != original[:i]+'新'+original[i:]:
            errors.append(('insert', original, item))
print(json.dumps({'examples':len(rows),'insertions':sum(len(row['inserts']) for row in rows),'failures':errors[:10],'failure_count':len(errors)},ensure_ascii=False,indent=2))
raise SystemExit(bool(errors))
