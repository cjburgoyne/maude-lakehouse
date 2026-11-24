import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from maude_lakehouse.cli import main


def test_land_command_lands_file_and_prints_json(tmp_path: Path, capsys):
    source = tmp_path / "foidevadd.zip"
    with ZipFile(source, "w") as archive:
        archive.writestr("sample.txt", "MDR_REPORT_KEY|TEXT\n1|hello\n")
    project_root = tmp_path / "project"
    project_root.mkdir()

    exit_code = main(["land", str(source), "--dataset", "device", "--project-root", str(project_root)])

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dataset"] == "device"
    assert result["new_version"] is True
    assert (project_root / result["object_path"]).is_file()


def test_land_requires_dataset(tmp_path: Path):
    with pytest.raises(SystemExit):
        main(["land", str(tmp_path / "x.zip")])
