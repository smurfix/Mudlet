from setuptools import setup, find_packages

setup(
    name="mudtest",
    version="1.2.3",
    description="Test code for Mudlet",
    url="https://github.com/Mudlet/Mudlet",
    author="Matthias Urlichs",
    author_email="matthias@urlichs.de",
    license="GPL v2 or later",
    packages=["mudtest"],
    include_package_data=True,
    install_requires=[
        "anyio>=2,<3",
        "trio>=0.17",
        "asyncclick>=7.1.2.3,<8",
        "mudpyc>=0.1.6",
        "asynctelnet>=0.2.3",
        "pyyaml",
        "pynput",
        "greenback>=0.3",
    ],
    python_requires=">=3.7",
)

