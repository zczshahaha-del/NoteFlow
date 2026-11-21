"""Audit the npm lock and the active project venv against public advisories."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not shutil.which('uv'):
        raise SystemExit('需要 uv 运行固定版本 pip-audit；请按 README 准备本地工具。')
    npm = subprocess.run(['npm','audit','--json','--registry=https://registry.npmjs.org'],cwd=ROOT,capture_output=True,text=True)
    python = subprocess.run(['uv','tool','run','--from','pip-audit==2.10.1','pip-audit','--path',sysconfig.get_paths()['purelib'],
                             '--format','json','--progress-spinner','off'],cwd=ROOT,capture_output=True,text=True)
    try:
        npm_report=json.loads(npm.stdout); python_report=json.loads(python.stdout)
    except json.JSONDecodeError:
        raise SystemExit('依赖审计未完成，请检查网络或工具输出；不能计为通过。')
    if 'error' in npm_report or 'dependencies' not in python_report:
        raise SystemExit('依赖公告查询失败，不能计为通过。')
    summary={'npm':npm_report['metadata']['vulnerabilities'],
             'pythonAffectedPackages':[{'name':p['name'],'version':p['version'],'ids':[v['id'] for v in p['vulns']]}
                                       for p in python_report['dependencies'] if p.get('vulns')],
             'pythonSkippedPackages':[p['name'] for p in python_report['dependencies'] if p.get('skip_reason')]}
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 1 if npm.returncode or python.returncode or summary['pythonSkippedPackages'] else 0

if __name__=='__main__':raise SystemExit(main())
