import pytest

from codesync import envfile
from codesync.cli import main
from codesync.envfile import read_env_file, setting
from codesync.llm import ProviderError, create_client
from codesync.sandbox import clean_environment


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(envfile, "ENV_FILE", path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    return path


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("KEY=abc", "abc"),
        ("KEY = abc", "abc"),
        ('KEY="abc"', "abc"),
        ("KEY='abc'", "abc"),
        ("export KEY=abc", "abc"),
        ("KEY=abc # my key", "abc"),
        ('KEY="abc # kept"', "abc # kept"),
        ("KEY=a=b", "a=b"),
        ("KEY=", ""),
    ],
)
def test_a_line_is_read_the_usual_way(tmp_path, line, expected):
    path = tmp_path / ".env"
    path.write_text(f"{line}\n", encoding="utf-8")

    assert read_env_file(path)["KEY"] == expected


def test_comments_and_blank_lines_are_skipped(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# a note\n\nnot a setting\nKEY=abc\n", encoding="utf-8")

    assert read_env_file(path) == {"KEY": "abc"}


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_the_ways_windows_saves_a_file_all_work(tmp_path, encoding):
    path = tmp_path / ".env"
    path.write_bytes("KEY=abc\r\nOTHER=def\r\n".encode(encoding))

    assert read_env_file(path) == {"KEY": "abc", "OTHER": "def"}


def test_a_missing_file_holds_nothing(tmp_path):
    assert read_env_file(tmp_path / ".env") == {}


def test_a_key_in_the_file_reaches_the_model_client(env_file):
    env_file.write_text("GEMINI_API_KEY=from-file\nGEMINI_MODEL=file-model\n")

    client = create_client()

    assert client._api_key == "from-file"
    assert client.model == "file-model"
    client.close()


def test_the_environment_overrides_the_file(env_file, monkeypatch):
    env_file.write_text("GEMINI_API_KEY=from-file\n")
    monkeypatch.setenv("GEMINI_API_KEY", "from-terminal")

    assert setting("GEMINI_API_KEY") == "from-terminal"


def test_an_empty_key_in_the_file_counts_as_no_key(env_file):
    env_file.write_text("GEMINI_API_KEY=\n")

    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        create_client()


def test_the_error_says_where_the_key_goes(env_file):
    with pytest.raises(ProviderError) as caught:
        create_client()

    assert str(env_file) in str(caught.value)


def test_a_key_in_the_file_never_enters_the_environment(env_file, tmp_path):
    env_file.write_text("GEMINI_API_KEY=from-file\n")

    create_client().close()

    assert "GEMINI_API_KEY" not in clean_environment(tmp_path)
    assert setting("GEMINI_API_KEY") == "from-file"


def test_a_env_file_in_the_current_folder_is_ignored(env_file, tmp_path, monkeypatch):
    project = tmp_path / "someone-elses-project"
    project.mkdir()
    (project / ".env").write_text("GEMINI_BASE_URL=https://evil.example\n")
    monkeypatch.chdir(project)

    assert setting("GEMINI_BASE_URL") is None


def test_check_uses_the_key_in_the_file(env_file, capsys, monkeypatch, annotator):
    env_file.write_text("GEMINI_API_KEY=AIzaabc12\n")
    client = annotator()
    client.base_url = "https://gemini.example"
    monkeypatch.setattr("codesync.cliargs.create_client", lambda **_: client)

    assert main(["check"]) == 0

    assert "key      GEMINI_API_KEY, 9 characters" in capsys.readouterr().out


def test_the_real_key_file_is_hidden_from_the_tests():
    assert not envfile.ENV_FILE.exists()
