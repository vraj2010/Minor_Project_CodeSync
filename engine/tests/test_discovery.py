import pytest

from codesync.discovery import discover, module_name


def test_discovers_every_module_in_the_sample_project(sample_project):
    found = discover(sample_project)

    assert {source.module for source in found} == {
        "app",
        "app.models",
        "app.services",
        "app.services.parser",
        "report",
    }


def test_records_relative_paths_and_package_files(sample_project):
    found = {source.module: source for source in discover(sample_project)}

    assert found["app.services.parser"].relpath == "app/services/parser.py"
    assert found["app"].is_package is True
    assert found["app.models"].is_package is False


def test_results_are_ordered_by_path(sample_project):
    paths = [source.relpath for source in discover(sample_project)]

    assert paths == sorted(paths)


def test_skips_generated_and_environment_directories(write_project):
    root = write_project(
        {
            "app/main.py": "value = 1\n",
            "__pycache__/main.py": "value = 1\n",
            ".venv/lib/vendored.py": "value = 1\n",
            "build/generated.py": "value = 1\n",
            "notes.txt": "not python\n",
        }
    )

    assert [source.relpath for source in discover(root)] == ["app/main.py"]


def test_gitignored_files_are_skipped(write_project):
    root = write_project(
        {
            ".gitignore": "generated/\nscratch.py\n",
            "app/main.py": "value = 1\n",
            "app/scratch.py": "value = 1\n",
            "generated/models.py": "value = 1\n",
        }
    )

    assert [source.relpath for source in discover(root)] == ["app/main.py"]


def test_gitignore_can_be_disabled(write_project):
    root = write_project(
        {
            ".gitignore": "generated/\n",
            "generated/models.py": "value = 1\n",
        }
    )

    assert [source.relpath for source in discover(root, use_gitignore=False)] == [
        "generated/models.py"
    ]


def test_module_names_ignore_a_src_directory(write_project):
    root = write_project(
        {
            "src/mypkg/__init__.py": "",
            "src/mypkg/core.py": "value = 1\n",
            "src/mypkg/io/__init__.py": "",
            "src/mypkg/io/reader.py": "value = 1\n",
        }
    )

    assert {source.module for source in discover(root)} == {
        "mypkg",
        "mypkg.core",
        "mypkg.io",
        "mypkg.io.reader",
    }


def test_module_name_of_a_file_outside_any_package(write_project):
    root = write_project({"scripts/release.py": "value = 1\n"})

    assert module_name(root / "scripts" / "release.py", root) == "release"


def test_root_level_package_file_keeps_the_directory_name(write_project):
    root = write_project({"__init__.py": "", "core.py": "value = 1\n"})

    assert module_name(root / "__init__.py", root) == root.name
    assert module_name(root / "core.py", root) == "core"


def test_a_missing_root_is_rejected(tmp_path):
    with pytest.raises(NotADirectoryError):
        discover(tmp_path / "absent")
