import pytest


@pytest.fixture(autouse=True)
def _isolated_control_watch_home(tmp_path_factory, monkeypatch):
    """控制面守卫会在 shell 命令前后比对 ~/.zshrc 等文件：测试里指向一个空的临时目录，不碰开发者自己的家目录。"""
    home = tmp_path_factory.mktemp("control-home")
    monkeypatch.setattr("bunnybyte.core.control_watch._home", lambda: home)
