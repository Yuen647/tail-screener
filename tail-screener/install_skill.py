"""Install the local skill entry without scanning project archives as skills."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile
import os
import stat


KIND='tail_screener_local_binding_v1'
AGENT_DIRECTORIES={'codex':'.agents/skills','claude-code':'.claude/skills'}


def default_skills_dir(agent):
    return Path.home()/AGENT_DIRECTORIES[agent]


def is_link(path):
    return path.is_symlink() or (path.exists() and bool(getattr(path.lstat(),'st_file_attributes',0) & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0)))


def install(source,skills_dir,*,update=False):
    source=Path(source).resolve();skills_dir=Path(skills_dir).expanduser().resolve()
    body=(source/'SKILL.md').read_text(encoding='utf-8-sig')
    if not re.search(r'(?m)^name:\s*tail-screener\s*$',body):raise ValueError('Source is not the tail-screener skill')
    for relative in ('scripts/screen.py','screen.cmd','agents/openai.yaml','skill-install/SKILL.md.template'):
        if not (source/relative).is_file():raise ValueError('Missing skill resource: '+relative)
    target=skills_dir/'tail-screener'
    if target==source or target.is_relative_to(source):raise ValueError('Install outside the project to avoid recursive discovery')
    if is_link(target):raise ValueError('Existing skill is a link; it was not changed')
    files={'SKILL.md':(source/'skill-install/SKILL.md.template').read_bytes(),
           'agents/openai.yaml':(source/'agents/openai.yaml').read_bytes()}
    if target.exists():
        if not update:raise ValueError('Skill already exists; use --update for an unchanged managed installation')
        old=json.loads((target/'runtime-location.json').read_text(encoding='utf-8'))
        if old.get('schema_version')!=1 or old.get('installation_kind')!=KIND:raise ValueError('Existing skill is not managed by this installer')
        if set(old['owned_files'])!=set(files):raise ValueError('Installed ownership metadata is invalid')
        for relative,sha in old['owned_files'].items():
            if relative not in files or hashlib.sha256((target/relative).read_bytes()).hexdigest()!=sha:
                raise ValueError('Installed file changed; nothing overwritten: '+relative)
    target.mkdir(parents=True,exist_ok=True)
    settings={'schema_version':1,'installation_kind':KIND,'project_root':str(source),
              'owned_files':{rel:hashlib.sha256(raw).hexdigest() for rel,raw in files.items()}}
    files['runtime-location.json']=(json.dumps(settings,ensure_ascii=False,indent=2)+'\n').encode()
    for relative,raw in files.items():
        destination=target/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent,delete=False) as stream:
            temporary=Path(stream.name);stream.write(raw)
        try:os.replace(temporary,destination)
        finally:temporary.unlink(missing_ok=True)
    return {'skill':'tail-screener','install_path':str(target),'project_root':str(source),
            'mode':'managed_local_binding','source_archives_exposed_to_discovery':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--agent',choices=tuple(AGENT_DIRECTORIES),default='codex',help='Select the host discovery directory; execution is agent independent')
    parser.add_argument('--skills-dir',type=Path,help='Override the host skill parent directory')
    parser.add_argument('--update',action='store_true')
    args=parser.parse_args()
    try:result=install(args.source,args.skills_dir or default_skills_dir(args.agent),update=args.update)
    except (OSError,ValueError,KeyError) as exc:parser.error(str(exc))
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
