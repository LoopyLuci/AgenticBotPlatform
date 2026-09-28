"""Starting points: complete, working project skeletons, and the files every project needs (.gitignore, LICENSE,
.editorconfig, README, CI workflows, Dockerfiles).

Each template is a small set of files that builds and runs as generated: a test is included wherever the language has
a standard test runner, so the first thing an agent can do after scaffolding is run it.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Optional

from abp_toolkit.registry import ToolkitError, action, group
from abp_toolkit.util import inside, rel

group("generate", "Scaffold projects (Python, Node/TS, React, Electron, Rust, Go, PowerShell, VS Code/Chrome extensions, PyQt...) and project files")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    if not s:
        raise ToolkitError("name must contain letters or digits")
    return s


def _ident(name: str) -> str:
    return _slug(name).replace("-", "_")


def _pascal(name: str) -> str:
    return "".join(p.capitalize() for p in _slug(name).split("-"))


GITIGNORE = {
    "python": "__pycache__/\n*.py[cod]\n.venv/\nvenv/\nbuild/\ndist/\n*.egg-info/\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\n.coverage\nhtmlcov/\n",
    "node": "node_modules/\ndist/\nbuild/\n.env\n.env.local\nnpm-debug.log*\ncoverage/\n*.tsbuildinfo\n",
    "rust": "target/\n**/*.rs.bk\n",
    "go": "/bin/\n*.exe\n*.test\n*.out\ncoverage.txt\n",
    "dotnet": "bin/\nobj/\n*.user\n.vs/\n",
    "java": "target/\nbuild/\n.gradle/\n*.class\n",
    "common": ".DS_Store\nThumbs.db\n.idea/\n.vscode/*\n!.vscode/extensions.json\n*.log\n.env\n",
}

LICENSES = {
    "MIT": ("MIT License\n\nCopyright (c) {year} {holder}\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\n"
            "of this software and associated documentation files (the \"Software\"), to deal\nin the Software without restriction, "
            "including without limitation the rights\nto use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
            "copies of the Software, and to permit persons to whom the Software is\nfurnished to do so, subject to the following conditions:\n\n"
            "The above copyright notice and this permission notice shall be included in all\ncopies or substantial portions of the Software.\n\n"
            "THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF "
            "MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS OR COPYRIGHT HOLDERS BE LIABLE "
            "FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION "
            "WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE.\n"),
    "Apache-2.0": ("Copyright {year} {holder}\n\nLicensed under the Apache License, Version 2.0 (the \"License\");\nyou may not use this file except "
                   "in compliance with the License.\nYou may obtain a copy of the License at\n\n    http://www.apache.org/licenses/LICENSE-2.0\n\n"
                   "Unless required by applicable law or agreed to in writing, software\ndistributed under the License is distributed on an \"AS IS\" "
                   "BASIS,\nWITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.\nSee the License for the specific language "
                   "governing permissions and\nlimitations under the License.\n"),
    "BSD-3-Clause": ("BSD 3-Clause License\n\nCopyright (c) {year}, {holder}\n\nRedistribution and use in source and binary forms, with or "
                     "without\nmodification, are permitted provided that the following conditions are met:\n\n1. Redistributions of source "
                     "code must retain the above copyright notice, this\n   list of conditions and the following disclaimer.\n\n2. "
                     "Redistributions in binary form must reproduce the above copyright notice,\n   this list of conditions and the following "
                     "disclaimer in the documentation\n   and/or other materials provided with the distribution.\n\n3. Neither the name of the "
                     "copyright holder nor the names of its\n   contributors may be used to endorse or promote products derived from\n   this "
                     "software without specific prior written permission.\n\nTHIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "
                     "\"AS IS\"\nAND ANY EXPRESS OR IMPLIED WARRANTIES ARE DISCLAIMED. IN NO EVENT SHALL THE\nCOPYRIGHT HOLDER OR CONTRIBUTORS BE "
                     "LIABLE FOR ANY DAMAGES ARISING IN ANY WAY\nOUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.\n"),
    "Unlicense": ("This is free and unencumbered software released into the public domain.\n\nAnyone is free to copy, modify, publish, use, "
                  "compile, sell, or\ndistribute this software, either in source code form or as a compiled\nbinary, for any purpose, commercial or "
                  "non-commercial, and by any\nmeans.\n\nFor more information, please refer to <https://unlicense.org>\n"),
}

EDITORCONFIG = ("root = true\n\n[*]\ncharset = utf-8\nend_of_line = lf\ninsert_final_newline = true\ntrim_trailing_whitespace = true\n"
                "indent_style = space\nindent_size = 2\n\n[*.{py,rs,go}]\nindent_size = 4\n\n[*.go]\nindent_style = tab\n\n"
                "[*.{bat,cmd,ps1}]\nend_of_line = crlf\nindent_size = 4\n\n[*.md]\ntrim_trailing_whitespace = false\n")


def _templates(name: str, description: str, author: str) -> dict[str, dict[str, str]]:
    slug, ident, pascal = _slug(name), _ident(name), _pascal(name)
    readme = lambda how: f"# {name}\n\n{description}\n\n## Development\n\n```\n{how}\n```\n"  # noqa: E731
    return {
        "python-package": {
            "pyproject.toml": f'[build-system]\nrequires = ["setuptools>=68", "wheel"]\nbuild-backend = "setuptools.build_meta"\n\n[project]\nname = "{slug}"\nversion = "0.1.0"\ndescription = "{description}"\nreadme = "README.md"\nrequires-python = ">=3.10"\nauthors = [{{name = "{author}"}}]\ndependencies = []\n\n[project.optional-dependencies]\ndev = ["pytest>=8", "ruff>=0.5"]\n\n[tool.setuptools.packages.find]\nwhere = ["src"]\n\n[tool.ruff]\nline-length = 110\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["src"]\n',
            f"src/{ident}/__init__.py": f'"""{description}"""\n\n__version__ = "0.1.0"\n\n\ndef greet(name: str) -> str:\n    """A friendly greeting."""\n    return f"Hello, {{name}}!"\n',
            "tests/test_basic.py": f"from {ident} import greet\n\n\ndef test_greet():\n    assert greet(\"world\") == \"Hello, world!\"\n",
            "README.md": readme("python -m venv .venv\n.venv/Scripts/pip install -e .[dev]   # (bin/pip on Linux/macOS)\npytest"),
            ".gitignore": GITIGNORE["python"] + GITIGNORE["common"],
        },
        "python-cli": {
            "pyproject.toml": f'[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n\n[project]\nname = "{slug}"\nversion = "0.1.0"\ndescription = "{description}"\nrequires-python = ">=3.10"\n\n[project.scripts]\n{slug} = "{ident}.cli:main"\n\n[tool.setuptools.packages.find]\nwhere = ["src"]\n\n[tool.pytest.ini_options]\npythonpath = ["src"]\n',
            f"src/{ident}/__init__.py": f'"""{description}"""\n__version__ = "0.1.0"\n',
            f"src/{ident}/__main__.py": "import sys\n\nfrom .cli import main\n\nsys.exit(main())\n",
            f"src/{ident}/cli.py": f'"""The {slug} command line."""\nfrom __future__ import annotations\n\nimport argparse\nimport sys\n\nfrom . import __version__\n\n\ndef main(argv: list[str] | None = None) -> int:\n    parser = argparse.ArgumentParser(prog="{slug}", description="{description}")\n    parser.add_argument("--version", action="version", version=__version__)\n    sub = parser.add_subparsers(dest="command", required=True)\n    hello = sub.add_parser("hello", help="say hello")\n    hello.add_argument("name", nargs="?", default="world")\n    args = parser.parse_args(argv)\n    if args.command == "hello":\n        print(f"Hello, {{args.name}}!")\n    return 0\n\n\nif __name__ == "__main__":\n    sys.exit(main())\n',
            "tests/test_cli.py": f"from {ident}.cli import main\n\n\ndef test_hello(capsys):\n    assert main([\"hello\", \"there\"]) == 0\n    assert capsys.readouterr().out.strip() == \"Hello, there!\"\n",
            "README.md": readme(f"pip install -e .\n{slug} hello"),
            ".gitignore": GITIGNORE["python"] + GITIGNORE["common"],
        },
        "fastapi-service": {
            "requirements.txt": "fastapi>=0.110\nuvicorn[standard]>=0.29\npydantic>=2\n\n# tests\npytest>=8\nhttpx>=0.27\n",
            "app/__init__.py": "",
            "app/main.py": f'"""{description}"""\nfrom fastapi import FastAPI, HTTPException\nfrom pydantic import BaseModel\n\napp = FastAPI(title="{name}")\n\n\nclass Item(BaseModel):\n    name: str\n    price: float\n\n\nITEMS: dict[int, Item] = {{}}\n\n\n@app.get("/health")\ndef health() -> dict:\n    return {{"ok": True}}\n\n\n@app.post("/items", status_code=201)\ndef create(item: Item) -> dict:\n    item_id = len(ITEMS) + 1\n    ITEMS[item_id] = item\n    return {{"id": item_id, **item.model_dump()}}\n\n\n@app.get("/items/{{item_id}}")\ndef read(item_id: int) -> Item:\n    if item_id not in ITEMS:\n        raise HTTPException(404, "no such item")\n    return ITEMS[item_id]\n',
            "tests/test_api.py": "from fastapi.testclient import TestClient\n\nfrom app.main import app\n\nclient = TestClient(app)\n\n\ndef test_health():\n    assert client.get(\"/health\").json() == {\"ok\": True}\n\n\ndef test_items():\n    r = client.post(\"/items\", json={\"name\": \"a\", \"price\": 1.5})\n    assert r.status_code == 201\n    assert client.get(f\"/items/{r.json()['id']}\").json()[\"name\"] == \"a\"\n    assert client.get(\"/items/999\").status_code == 404\n",
            "Dockerfile": "FROM python:3.12-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY app ./app\nEXPOSE 8000\nCMD [\"uvicorn\", \"app.main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"]\n",
            "README.md": readme("pip install -r requirements.txt\nuvicorn app.main:app --reload\npytest"),
            ".gitignore": GITIGNORE["python"] + GITIGNORE["common"],
        },
        "node-cli": {
            "package.json": f'{{\n  "name": "{slug}",\n  "version": "0.1.0",\n  "description": "{description}",\n  "type": "module",\n  "bin": {{ "{slug}": "./bin/cli.js" }},\n  "scripts": {{ "test": "node --test" }},\n  "engines": {{ "node": ">=18" }},\n  "license": "MIT"\n}}\n',
            "bin/cli.js": "#!/usr/bin/env node\nimport { greet } from '../src/index.js';\n\nconst [, , name = 'world'] = process.argv;\nconsole.log(greet(name));\n",
            "src/index.js": "export function greet(name) {\n  return `Hello, ${name}!`;\n}\n",
            "test/index.test.js": "import { test } from 'node:test';\nimport assert from 'node:assert/strict';\nimport { greet } from '../src/index.js';\n\ntest('greet', () => {\n  assert.equal(greet('world'), 'Hello, world!');\n});\n",
            "README.md": readme("npm test\nnode bin/cli.js you"),
            ".gitignore": GITIGNORE["node"] + GITIGNORE["common"],
        },
        "typescript-library": {
            "package.json": f'{{\n  "name": "{slug}",\n  "version": "0.1.0",\n  "description": "{description}",\n  "type": "module",\n  "main": "dist/index.js",\n  "types": "dist/index.d.ts",\n  "scripts": {{\n    "build": "tsc",\n    "test": "npm run build && node --test dist/"\n  }},\n  "devDependencies": {{ "typescript": "^5.4.0", "@types/node": "^20.0.0" }},\n  "license": "MIT"\n}}\n',
            "tsconfig.json": '{\n  "compilerOptions": {\n    "target": "ES2022",\n    "module": "NodeNext",\n    "moduleResolution": "NodeNext",\n    "declaration": true,\n    "outDir": "dist",\n    "rootDir": "src",\n    "strict": true,\n    "skipLibCheck": true\n  },\n  "include": ["src"]\n}\n',
            "src/index.ts": "export function sum(values: number[]): number {\n  return values.reduce((a, b) => a + b, 0);\n}\n",
            "src/index.test.ts": "import { test } from 'node:test';\nimport assert from 'node:assert/strict';\nimport { sum } from './index.js';\n\ntest('sum', () => {\n  assert.equal(sum([1, 2, 3]), 6);\n});\n",
            "README.md": readme("npm install\nnpm test"),
            ".gitignore": GITIGNORE["node"] + GITIGNORE["common"],
        },
        "react-vite": {
            "package.json": f'{{\n  "name": "{slug}",\n  "private": true,\n  "version": "0.1.0",\n  "type": "module",\n  "scripts": {{\n    "dev": "vite",\n    "build": "tsc -b && vite build",\n    "preview": "vite preview"\n  }},\n  "dependencies": {{ "react": "^18.3.1", "react-dom": "^18.3.1" }},\n  "devDependencies": {{\n    "@types/react": "^18.3.3",\n    "@types/react-dom": "^18.3.0",\n    "@vitejs/plugin-react": "^4.3.1",\n    "typescript": "^5.5.3",\n    "vite": "^5.4.0"\n  }}\n}}\n',
            "index.html": f'<!doctype html>\n<html lang="en">\n  <head>\n    <meta charset="UTF-8" />\n    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n    <title>{name}</title>\n  </head>\n  <body>\n    <div id="root"></div>\n    <script type="module" src="/src/main.tsx"></script>\n  </body>\n</html>\n',
            "vite.config.ts": "import { defineConfig } from 'vite';\nimport react from '@vitejs/plugin-react';\n\nexport default defineConfig({ plugins: [react()] });\n",
            "tsconfig.json": '{\n  "compilerOptions": {\n    "target": "ES2020",\n    "lib": ["ES2020", "DOM", "DOM.Iterable"],\n    "module": "ESNext",\n    "moduleResolution": "bundler",\n    "jsx": "react-jsx",\n    "strict": true,\n    "noEmit": true,\n    "skipLibCheck": true\n  },\n  "include": ["src"]\n}\n',
            "src/main.tsx": "import { StrictMode } from 'react';\nimport { createRoot } from 'react-dom/client';\nimport App from './App';\nimport './styles.css';\n\ncreateRoot(document.getElementById('root')!).render(\n  <StrictMode>\n    <App />\n  </StrictMode>,\n);\n",
            "src/App.tsx": f"import {{ useState }} from 'react';\n\nexport default function App() {{\n  const [count, setCount] = useState(0);\n  return (\n    <main>\n      <h1>{name}</h1>\n      <p>{description}</p>\n      <button onClick={{() => setCount((c) => c + 1)}}>Clicked {{count}} times</button>\n    </main>\n  );\n}}\n",
            "src/styles.css": ":root { font-family: system-ui, sans-serif; color-scheme: light dark; }\nmain { max-width: 720px; margin: 4rem auto; padding: 0 1rem; }\nbutton { padding: .6rem 1rem; border-radius: 8px; border: 1px solid #8884; cursor: pointer; }\n",
            "README.md": readme("npm install\nnpm run dev"),
            ".gitignore": GITIGNORE["node"] + GITIGNORE["common"],
        },
        "electron-app": {
            "package.json": f'{{\n  "name": "{slug}",\n  "version": "0.1.0",\n  "description": "{description}",\n  "main": "main.js",\n  "scripts": {{ "start": "electron ." }},\n  "devDependencies": {{ "electron": "^31.0.0" }},\n  "license": "MIT"\n}}\n',
            "main.js": "const { app, BrowserWindow, ipcMain } = require('electron');\nconst path = require('node:path');\n\nfunction createWindow() {\n  const win = new BrowserWindow({\n    width: 1000,\n    height: 700,\n    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true },\n  });\n  win.loadFile('index.html');\n}\n\nipcMain.handle('app:version', () => app.getVersion());\napp.whenReady().then(() => {\n  createWindow();\n  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });\n});\napp.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });\n",
            "preload.js": "const { contextBridge, ipcRenderer } = require('electron');\n\ncontextBridge.exposeInMainWorld('api', { version: () => ipcRenderer.invoke('app:version') });\n",
            "index.html": f"<!doctype html>\n<html>\n  <head>\n    <meta charset=\"utf-8\" />\n    <meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'self'; script-src 'self'\" />\n    <title>{name}</title>\n  </head>\n  <body>\n    <h1>{name}</h1>\n    <p id=\"v\"></p>\n    <script src=\"renderer.js\"></script>\n  </body>\n</html>\n",
            "renderer.js": "window.api.version().then((v) => { document.getElementById('v').textContent = 'Version ' + v; });\n",
            "README.md": readme("npm install\nnpm start"),
            ".gitignore": GITIGNORE["node"] + GITIGNORE["common"],
        },
        "rust-cli": {
            "Cargo.toml": f'[package]\nname = "{slug}"\nversion = "0.1.0"\nedition = "2021"\ndescription = "{description}"\n\n[dependencies]\n',
            "src/main.rs": "fn greet(name: &str) -> String {\n    format!(\"Hello, {name}!\")\n}\n\nfn main() {\n    let name = std::env::args().nth(1).unwrap_or_else(|| \"world\".to_string());\n    println!(\"{}\", greet(&name));\n}\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n\n    #[test]\n    fn greets() {\n        assert_eq!(greet(\"world\"), \"Hello, world!\");\n    }\n}\n",
            "README.md": readme("cargo test\ncargo run -- you"),
            ".gitignore": GITIGNORE["rust"] + GITIGNORE["common"],
        },
        "go-cli": {
            "go.mod": f"module example.com/{slug}\n\ngo 1.21\n",
            "main.go": "package main\n\nimport (\n\t\"fmt\"\n\t\"os\"\n)\n\nfunc greet(name string) string {\n\treturn fmt.Sprintf(\"Hello, %s!\", name)\n}\n\nfunc main() {\n\tname := \"world\"\n\tif len(os.Args) > 1 {\n\t\tname = os.Args[1]\n\t}\n\tfmt.Println(greet(name))\n}\n",
            "main_test.go": "package main\n\nimport \"testing\"\n\nfunc TestGreet(t *testing.T) {\n\tif got := greet(\"world\"); got != \"Hello, world!\" {\n\t\tt.Fatalf(\"got %q\", got)\n\t}\n}\n",
            "README.md": readme("go test ./...\ngo run . you"),
            ".gitignore": GITIGNORE["go"] + GITIGNORE["common"],
        },
        "powershell-module": {
            f"{pascal}/{pascal}.psd1": f"@{{\n    RootModule        = '{pascal}.psm1'\n    ModuleVersion     = '0.1.0'\n    GUID              = '{__import__('uuid').uuid4()}'\n    Author            = '{author}'\n    Description       = '{description}'\n    PowerShellVersion = '5.1'\n    FunctionsToExport = @('Get-Greeting')\n}}\n",
            f"{pascal}/{pascal}.psm1": "Set-StrictMode -Version Latest\n\nfunction Get-Greeting {\n    <#\n    .SYNOPSIS\n    Returns a greeting.\n    .EXAMPLE\n    Get-Greeting -Name you\n    #>\n    [CmdletBinding()]\n    param([string]$Name = 'world')\n    \"Hello, $Name!\"\n}\n\nExport-ModuleMember -Function Get-Greeting\n",
            f"Tests/{pascal}.Tests.ps1": f"BeforeAll {{ Import-Module \"$PSScriptRoot/../{pascal}/{pascal}.psd1\" -Force }}\n\nDescribe 'Get-Greeting' {{\n    It 'greets' {{ Get-Greeting -Name you | Should -Be 'Hello, you!' }}\n}}\n",
            "README.md": readme(f"Import-Module ./{pascal}/{pascal}.psd1\nGet-Greeting\nInvoke-Pester ./Tests"),
            ".gitignore": GITIGNORE["common"],
        },
        "vscode-extension": {
            "package.json": f'{{\n  "name": "{slug}",\n  "displayName": "{name}",\n  "description": "{description}",\n  "version": "0.1.0",\n  "engines": {{ "vscode": "^1.85.0" }},\n  "main": "./extension.js",\n  "activationEvents": [],\n  "contributes": {{\n    "commands": [{{ "command": "{slug}.hello", "title": "{name}: Say hello" }}]\n  }},\n  "scripts": {{ "package": "npx @vscode/vsce package" }}\n}}\n',
            "extension.js": f"const vscode = require('vscode');\n\nfunction activate(context) {{\n  context.subscriptions.push(\n    vscode.commands.registerCommand('{slug}.hello', () => vscode.window.showInformationMessage('Hello from {name}!')),\n  );\n}}\n\nfunction deactivate() {{}}\n\nmodule.exports = {{ activate, deactivate }};\n",
            "README.md": readme("code --extensionDevelopmentPath=."),
            ".gitignore": GITIGNORE["node"] + "*.vsix\n" + GITIGNORE["common"],
        },
        "chrome-extension": {
            "manifest.json": f'{{\n  "manifest_version": 3,\n  "name": "{name}",\n  "description": "{description}",\n  "version": "0.1.0",\n  "action": {{ "default_popup": "popup.html" }},\n  "permissions": ["storage"]\n}}\n',
            "popup.html": f'<!doctype html>\n<html>\n  <body style="min-width:240px;font-family:system-ui">\n    <h3>{name}</h3>\n    <button id="b">Count</button> <span id="n">0</span>\n    <script src="popup.js"></script>\n  </body>\n</html>\n',
            "popup.js": "const n = document.getElementById('n');\nchrome.storage.local.get({ count: 0 }, ({ count }) => { n.textContent = count; });\ndocument.getElementById('b').addEventListener('click', () => {\n  const count = Number(n.textContent) + 1;\n  n.textContent = count;\n  chrome.storage.local.set({ count });\n});\n",
            "README.md": readme("chrome://extensions → Developer mode → Load unpacked → this folder"),
        },
        "pyqt-app": {
            "requirements.txt": "PyQt5>=5.15\n",
            "app.py": f'"""{description}"""\nimport sys\n\nfrom PyQt5.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton, QVBoxLayout, QWidget\n\n\nclass MainWindow(QMainWindow):\n    def __init__(self) -> None:\n        super().__init__()\n        self.setWindowTitle("{name}")\n        self.count = 0\n        self.label = QLabel("Clicked 0 times")\n        button = QPushButton("Click me")\n        button.clicked.connect(self.clicked)\n        layout = QVBoxLayout()\n        layout.addWidget(self.label)\n        layout.addWidget(button)\n        central = QWidget()\n        central.setLayout(layout)\n        self.setCentralWidget(central)\n\n    def clicked(self) -> None:\n        self.count += 1\n        self.label.setText(f"Clicked {{self.count}} times")\n\n\nif __name__ == "__main__":\n    app = QApplication(sys.argv)\n    window = MainWindow()\n    window.show()\n    sys.exit(app.exec_())\n',
            "README.md": readme("pip install -r requirements.txt\npython app.py"),
            ".gitignore": GITIGNORE["python"] + GITIGNORE["common"],
        },
        "static-site": {
            "index.html": f'<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">\n  <title>{name}</title>\n  <meta name="description" content="{description}">\n  <link rel="stylesheet" href="styles.css">\n</head>\n<body>\n  <header><h1>{name}</h1><p>{description}</p></header>\n  <main id="app"></main>\n  <script src="app.js" type="module"></script>\n</body>\n</html>\n',
            "styles.css": ":root { --bg: #fff; --fg: #0f172a; --accent: #6366f1; font-family: system-ui, sans-serif; }\n@media (prefers-color-scheme: dark) { :root { --bg: #0f172a; --fg: #e2e8f0; } }\nbody { margin: 0; background: var(--bg); color: var(--fg); }\nheader, main { max-width: 760px; margin: 0 auto; padding: 2rem 1rem; }\na { color: var(--accent); }\n",
            "app.js": "document.getElementById('app').textContent = `Loaded at ${new Date().toLocaleTimeString()}`;\n",
            "README.md": readme("python -m http.server 8000"),
        },
        "batch-toolkit": {
            f"{slug}.cmd": f"@echo off\r\nsetlocal EnableExtensions\r\nrem {description}\r\nif \"%~1\"==\"\" goto :usage\r\nif /i \"%~1\"==\"hello\" goto :hello\r\ngoto :usage\r\n\r\n:hello\r\necho Hello, %~2!\r\nexit /b 0\r\n\r\n:usage\r\necho Usage: %~n0 hello NAME\r\nexit /b 1\r\n",
            f"{slug}.vbs": f"Option Explicit\r\n' {description}\r\nDim name\r\nIf WScript.Arguments.Count > 0 Then name = WScript.Arguments(0) Else name = \"world\"\r\nWScript.Echo \"Hello, \" & name & \"!\"\r\n",
            f"{slug}.ps1": f"<#\n.SYNOPSIS\n{description}\n#>\n[CmdletBinding()]\nparam([string]$Name = 'world')\nSet-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\nWrite-Output \"Hello, $Name!\"\n",
            "README.md": readme(f"{slug}.cmd hello you\ncscript //nologo {slug}.vbs you\npwsh -File {slug}.ps1 -Name you"),
        },
    }


@action("generate.templates")
def templates() -> dict:
    """The project templates available, with the files each one creates"""
    return {k: sorted(v) for k, v in _templates("example", "An example project.", "you").items()}


@action("generate.project", writes=True)
def project(workspace: Path, template: str, name: str, folder: str = "", description: str = "",
            author: str = "", license: str = "MIT", overwrite: bool = False) -> dict:
    """Create a complete, working project from a template (with a test where the language has a runner)

    template: python-package, python-cli, fastapi-service, node-cli, typescript-library, react-vite, electron-app, rust-cli, go-cli, powershell-module, vscode-extension, chrome-extension, pyqt-app, static-site, batch-toolkit
    name: the project's name
    folder: where to create it inside the working folder (default: a folder named after it)
    license: MIT, Apache-2.0, BSD-3-Clause, Unlicense or none
    """
    all_t = _templates(name, description or f"{name}.", author or "The authors")
    if template not in all_t:
        raise ToolkitError(f"template is one of {', '.join(sorted(all_t))}")
    root = inside(workspace, folder or _slug(name))
    files = dict(all_t[template])
    if license != "none":
        if license not in LICENSES:
            raise ToolkitError(f"license is one of {', '.join(LICENSES)} or none")
        files["LICENSE"] = LICENSES[license].format(year=datetime.date.today().year, holder=author or "The authors")
    files.setdefault(".editorconfig", EDITORCONFIG)
    clashes = [f for f in files if (root / f).exists()]
    if clashes and not overwrite:
        raise ToolkitError(f"{len(clashes)} file(s) already exist, e.g. {clashes[0]} (overwrite=true replaces them)")
    for fname, content in files.items():
        p = root / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
    return {"template": template, "folder": rel(workspace, root), "files": sorted(files)}


@action("generate.file", writes=True)
def project_file(workspace: Path, kind: str, folder: str = ".", languages: Optional[list[str]] = None,
                 holder: str = "", license: str = "MIT", name: str = "", overwrite: bool = False) -> dict:
    """One project file: gitignore (for the languages given), license, editorconfig, readme, dockerfile, github-ci, dependabot

    kind: gitignore, license, editorconfig, readme, dockerfile-python, dockerfile-node, github-ci-python, github-ci-node, dependabot
    folder: where to write it inside the working folder
    languages: for gitignore: python, node, rust, go, dotnet, java
    """
    d = inside(workspace, folder)
    d.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    if kind == "gitignore":
        langs = languages or ["python", "node"]
        bad = [l for l in langs if l not in GITIGNORE]
        if bad:
            raise ToolkitError(f"unknown: {bad}; one of {', '.join(k for k in GITIGNORE if k != 'common')}")
        files[".gitignore"] = "".join(f"# {l}\n{GITIGNORE[l]}\n" for l in langs) + "# editors & OS\n" + GITIGNORE["common"]
    elif kind == "license":
        if license not in LICENSES:
            raise ToolkitError(f"license is one of {', '.join(LICENSES)}")
        files["LICENSE"] = LICENSES[license].format(year=datetime.date.today().year, holder=holder or "The authors")
    elif kind == "editorconfig":
        files[".editorconfig"] = EDITORCONFIG
    elif kind == "readme":
        title = name or Path(d).name
        files["README.md"] = (f"# {title}\n\nWhat it is, in one sentence.\n\n## Install\n\n```\n...\n```\n\n## Use\n\n```\n...\n```\n\n"
                              "## Develop\n\n```\n...\n```\n\n## License\n\n" + license + "\n")
    elif kind == "dockerfile-python":
        files["Dockerfile"] = ("FROM python:3.12-slim AS base\nENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1\nWORKDIR /app\n"
                               "COPY requirements.txt .\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY . .\n"
                               "RUN useradd --create-home app && chown -R app /app\nUSER app\nCMD [\"python\", \"main.py\"]\n")
        files[".dockerignore"] = ".git\n.venv\n__pycache__\n*.pyc\n.pytest_cache\n"
    elif kind == "dockerfile-node":
        files["Dockerfile"] = ("FROM node:20-alpine AS deps\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci --omit=dev\n\n"
                               "FROM node:20-alpine\nWORKDIR /app\nCOPY --from=deps /app/node_modules ./node_modules\nCOPY . .\n"
                               "USER node\nCMD [\"node\", \"index.js\"]\n")
        files[".dockerignore"] = ".git\nnode_modules\nnpm-debug.log\n"
    elif kind == "github-ci-python":
        files[".github/workflows/ci.yml"] = ("name: CI\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n"
                                             "      matrix:\n        os: [ubuntu-latest, windows-latest]\n        python: ['3.11', '3.12']\n    steps:\n"
                                             "      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n        with:\n          python-version: ${{ matrix.python }}\n"
                                             "      - run: pip install -e .[dev] || pip install -r requirements.txt pytest ruff\n      - run: ruff check .\n      - run: pytest\n")
    elif kind == "github-ci-node":
        files[".github/workflows/ci.yml"] = ("name: CI\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
                                             "      - uses: actions/checkout@v4\n      - uses: actions/setup-node@v4\n        with:\n          node-version: 20\n          cache: npm\n"
                                             "      - run: npm ci\n      - run: npm test\n")
    elif kind == "dependabot":
        files[".github/dependabot.yml"] = ("version: 2\nupdates:\n" + "".join(
            f"  - package-ecosystem: {eco}\n    directory: /\n    schedule:\n      interval: weekly\n"
            for eco in (languages or ["pip", "npm", "github-actions"])))
    else:
        raise ToolkitError("kind is gitignore, license, editorconfig, readme, dockerfile-python, dockerfile-node, "
                           "github-ci-python, github-ci-node or dependabot")
    written = []
    for fname, content in files.items():
        p = d / fname
        if p.exists() and not overwrite:
            raise ToolkitError(f"{rel(workspace, p)} already exists (overwrite=true replaces it)")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
        written.append(rel(workspace, p))
    return {"written": written}
