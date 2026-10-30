"""The bundled ctypes DLL targets Windows x64, independently of Python's ABI."""
from setuptools import setup
from setuptools.command.bdist_wheel import bdist_wheel


class WindowsWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self):
        return "py3", "none", "win_amd64"


setup(cmdclass={"bdist_wheel": WindowsWheel})
