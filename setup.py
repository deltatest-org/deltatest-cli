from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="deltatest-cli",
    version="0.4.36",
    description="Run only the tests affected by your code changes.",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Delta Contributors",
    packages=find_packages(exclude=["tests", "tests.*", "smart_test_runner", "smart_test_runner.*"]),
    package_dir={"delta": "delta"},
    install_requires=[
        "pytest>=7.0.0",
        "pytest-cov>=4.0.0",
        "coverage[toml]>=7.0.0",
        "requests>=2.25.0",
    ],
    entry_points={
        "console_scripts": [
            "delta=delta.cli:main",
        ],
        "pytest11": [
            "delta.pytest_plugin = delta.pytest_plugin",
        ],
    },
    python_requires=">=3.8",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
)
