# SPDX-FileCopyrightText: 2026 Nicholas Collins (K6NLC)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Setup script for ALERT2 decoder."""
from setuptools import setup, find_packages

with open('README.md', 'r') as f:
    long_description = f.read()

setup(
    name='alert2',
    version='0.9.1rc38',
    description='Pure-Python decoder for the ALERT2 hydrologic radio protocol',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='Nicholas Collins (K6NLC)',
    license='GPL-3.0-or-later',
    packages=find_packages(),
    package_data={
        'alert2': ['templates/*.html'],
    },
    include_package_data=True,
    install_requires=[
        'numpy>=1.20',
        'scipy>=1.6',
    ],
    python_requires='>=3.8',
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Science/Research',
        'License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Topic :: Communications :: Ham Radio',
        'Topic :: Scientific/Engineering',
    ],
    entry_points={
        'console_scripts': [
            'alert2-decode = alert2.cli:main',
            'alert2-tx = alert2.txcli:main',
            'alert2-txconsole = alert2.txconsole:main',
        ],
    },
)
