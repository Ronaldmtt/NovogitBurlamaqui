# make_bundle.py
import os, hashlib

ROOT = os.getcwd()
EXCLUDE_DIRS = {
    '.git','node_modules','venv','.venv','__pycache__','.pytest_cache',
    'dist','build','.next','.idea','.vscode',
    'rpa_screenshots','rpa_artifacts','uploads','logs',
    # 'instance',  # <- descomente para não incluir nada de instance/
}
INCLUDE_EXTS = {
    '.py','.ts','.tsx','.js','.jsx','.json','.yml','.yaml','.toml',
    '.ini','.cfg','.txt','.md','.sql','.html','.css','.env.example',
    '.jinja','.jinja2','.j2',
}
MAX_BYTES = 800_000  # sobe um pouco pra evitar truncar texto útil

def rel(p): 
    r = os.path.relpath(p, ROOT)
    return '.' if r == '.' else r.replace('\\','/')

lines = []

# 1) Árvore do projeto
lines.append("### TREE\n")
for base, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
    base_rel = rel(base)
    if base_rel != '.':
        lines.append(f"{base_rel}/")
    for f in sorted(files):
        lines.append(f"  {rel(os.path.join(base,f))}")

# 2) Conteúdo dos arquivos de texto
lines.append("\n\n### FILES\n")
rpa_sha = ""
files_count = 0

for base, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
    for f in sorted(files):
        p = os.path.join(base, f)
        ext = os.path.splitext(f)[1].lower()
        if ext in INCLUDE_EXTS and os.path.getsize(p) <= MAX_BYTES:
            with open(p, 'rb') as fh:
                raw = fh.read()
            # tenta decodificar como texto
            txt = None
            for enc in ('utf-8', 'latin-1'):
                try:
                    txt = raw.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if txt is None:
                continue
            sha = hashlib.sha1(raw).hexdigest()[:12]
            files_count += 1
            if rel(p).endswith("rpa.py"):
                rpa_sha = sha
            lines.append(
                f"\n----- FILE: {rel(p)}  (sha1:{sha}, bytes:{len(raw)}) -----\n{txt}"
            )

with open('bundle.txt','w',encoding='utf-8') as f:
    f.write('\n'.join(lines))

print(f"Gerado bundle.txt na pasta do projeto. Arquivos incluídos: {files_count}")
if rpa_sha:
    print(f"SHA do rpa.py no bundle: {rpa_sha}")
