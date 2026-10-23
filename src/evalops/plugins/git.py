"""git plugin: prompts + datasets stored as files in the repo (zero external services).

Prompt versions and dataset snapshots live under .evalops/store/ and are committed with the lock
files, so git *is* the registry. Aliases live in .evalops/store/prompts/<name>/aliases.json;
moving an alias is a commit (the release job pushes it).
"""
import json

from ..ports import DatasetStore, LoadedPrompt, Plugin, PromptStore


class GitPlugin(Plugin, PromptStore, DatasetStore):
    name = "git"
    provides = ("prompts", "datasets")

    def __init__(self, cfg, options):
        super().__init__(cfg, options)
        self.store = cfg.root / options.get("store", ".evalops/store")

    # ---- prompts ----
    def _pdir(self, name):
        d = self.store / "prompts" / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def register(self, name, template, tags):
        d = self._pdir(name)
        versions = [int(p.stem) for p in d.glob("*.json") if p.stem.isdigit()]
        v = str(max(versions, default=0) + 1)
        (d / f"{v}.json").write_text(json.dumps({"template": template, "tags": tags}, indent=2, sort_keys=True))
        return v

    def load(self, name, version):
        data = json.loads((self._pdir(name) / f"{version}.json").read_text())
        return LoadedPrompt(name=name, version=str(version), template=data["template"], native=data)

    def _aliases(self, name):
        p = self._pdir(name) / "aliases.json"
        return p, (json.loads(p.read_text()) if p.exists() else {})

    def get_alias(self, name, alias):
        return self._aliases(name)[1].get(alias)

    def set_alias(self, name, alias, version):
        p, data = self._aliases(name)
        data[alias] = str(version)
        p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    # ---- datasets ----
    def _dpath(self, physical):
        d = self.store / "datasets"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{physical}.jsonl"

    def exists(self, physical):
        return self._dpath(physical).exists()

    def write(self, physical, records):
        self._dpath(physical).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))

    def read(self, physical):
        return [json.loads(line) for line in self._dpath(physical).read_text().splitlines() if line.strip()]
