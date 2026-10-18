"""Build a distributable skill from the verified release manifest, without local state."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import runpy
import zipfile


PORTABLE_FILES = {'SKILL.md','agents/openai.yaml','assets/report.css','assets/icon.svg','config.json',
    'configs/account-config.json','requirements.txt','requirements-qlib.txt','screen.cmd',
    'install_skill.py','package_skill.py','skill-install/SKILL.md.template',
    'docs/environment-setup.md','docs/usage.md','docs/research-tools.md','docs/strategy-validation.md',
    'docs/kline-analysis.md','references/kline-validation.json',
    'docs/experimental-ranking.md','references/hgb33-validation.json','models/hgb33-fold2021.json',
    'references/account-workflow/protocol.json',
    'references/account-workflow/pre-evaluation-affordability-clarification.json',
    'references/stock-trend-template-source.json','examples/history-scope.json'}


def build(source, output, *, profile='portable'):
    if profile not in ('portable','full'):raise ValueError('Unknown package profile')
    source=Path(source).resolve();output=Path(output).resolve()
    manifest_path=source/'PACKAGE-MANIFEST.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    portable=manifest.get('portable_files',{})
    normalizer=None
    if portable or 'runtime_policy' in manifest:
        checks=runpy.run_path(str(Path(__file__).resolve().parent/'scripts/restore_support.py'))
        normalizer=checks['portability_entry']
        checks['host_metadata_files'](manifest)
        if set(portable)-set(manifest['files']):raise ValueError('Undeclared portable resource')
    entries=[]
    for relative, expected in manifest['files'].items():
        path=PurePosixPath(relative)
        if (path.is_absolute() or '..' in path.parts or '\\' in relative or ':' in relative
                or not path.parts or path.parts[0].startswith(('.runtime','.venv','.cache','.baselines'))
                or path.parts[0] in ('reports','dist') or path.name=='runtime-location.json'):
            raise ValueError('Local state or invalid release path: '+relative)
        candidate=source.joinpath(*path.parts)
        if not candidate.resolve().is_relative_to(source):raise ValueError('Release resource escapes project: '+relative)
        raw=candidate.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('Release checksum mismatch: '+relative)
        if relative in portable and portable[relative]!=normalizer(relative,raw):
            raise ValueError('Portable checksum mismatch: '+relative)
        if profile=='full' or relative in PORTABLE_FILES or (relative.startswith('scripts/') and relative.endswith('.py')):
            entries.append((relative,raw))
    if profile=='portable':
        selected={name for name,raw in entries}
        if 'scripts/screen.py' in manifest['files']:
            required={'SKILL.md','scripts/screen.py','requirements.txt',
                'references/account-workflow/protocol.json',
                'references/account-workflow/pre-evaluation-affordability-clarification.json',
                'references/stock-trend-template-source.json'}
            if required-selected:raise ValueError('Missing portable runtime resources: '+', '.join(sorted(required-selected)))
        manifest=dict(manifest)
        manifest['files']={name:digest for name,digest in manifest['files'].items() if name in selected}
        manifest['portable_files']={name:value for name,value in portable.items() if name in selected}
        manifest['package_variant']='portable-runtime'
        manifest['support_mode']='embedded-runtime'
        entries.append(('PACKAGE-MANIFEST.json',(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode('utf-8')))
    else:entries.append(('PACKAGE-MANIFEST.json',manifest_path.read_bytes()))
    output.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation prevents overwriting an existing deliverable.
    with output.open('xb') as stream:
        try:
            with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                for relative,raw in sorted(entries):
                    item=zipfile.ZipInfo('tail-screener/'+relative,date_time=(2020,1,1,0,0,0))
                    item.compress_type=zipfile.ZIP_DEFLATED
                    item.external_attr=0o100644 << 16
                    archive.writestr(item,raw)
        except Exception:
            stream.close();output.unlink();raise
    if profile=='portable' and output.stat().st_size>2*1024*1024:
        output.unlink()
        raise ValueError('Portable skill exceeds 2 MiB; move historical artifacts out of the upload package')
    return {'profile':profile,'files':len(entries),'bytes':output.stat().st_size,
            'expanded_bytes':sum(len(raw) for _,raw in entries),
            'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'output':str(output)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'dist/tail-screener-skill.zip')
    parser.add_argument('--profile',choices=('portable','full'),default='portable',help='默认轻量运行包；full保留原始历史附件')
    args=parser.parse_args()
    try:result=build(Path(__file__).resolve().parent,args.output,profile=args.profile)
    except (OSError,ValueError,KeyError) as exc:parser.error(str(exc))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
