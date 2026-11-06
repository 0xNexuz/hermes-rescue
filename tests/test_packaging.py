"""The package Hermes installs: manifest, README copy, and nothing the catalog refuses."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "hermes_rescue"


def test_plugin_readme_is_the_root_readme_without_the_development_section():
    root = (ROOT / "README.md").read_text()
    expected = root[:root.index("## Development")] + root[root.index("## License"):].replace(
        "[LICENSE](LICENSE)", "the LICENSE file in the repository")
    assert (PKG / "README.md").read_text() == expected


def test_no_binaries_bytecode_or_symlinks_in_the_package():
    for path in PKG.rglob("*"):
        if "__pycache__" in path.parts:
            continue
        assert not path.is_symlink(), path
        assert path.suffix not in {".pyc", ".pyo", ".so", ".dll", ".exe", ".wasm"}, path


def test_manifest_hooks_match_what_register_wires():
    import re
    manifest = (PKG / "plugin.yaml").read_text()
    declared = set(re.findall(r"^\s+- (\w+)$", manifest, re.M))
    init = (PKG / "__init__.py").read_text()
    registered = set(re.findall(r'register_hook\("(\w+)"', init))
    assert declared == registered


def test_desktop_id_matches_the_manifest_name():
    import re
    name = re.search(r"^name:\s*(\S+)", (PKG / "plugin.yaml").read_text(), re.M).group(1)
    js = (PKG / "desktop" / "plugin.js").read_text()
    assert f"const ID = '{name}'" in js
    assert f'"name": "{name}"' in (PKG / "dashboard" / "manifest.json").read_text()
