from setuptools import setup, find_packages
import os

version = '1.1.1'

requires = [
    'setuptools',
    'PyYAML',
    'gevent',
    'openprocurement_client==1.0b2',
    'esculator',
    'iso8601',
    'pytz',
    'zope.component',
]

test_requires = requires + [
    'webtest',
    'python-coveralls',
    'mock',
    'redis',
    'lazydb'
]

docs_requires = requires + [
    'sphinxcontrib-httpdomain',
]

entry_points = {
    'console_scripts': [
        'contracting_data_bridge = openprocurement.bridge.contracting.databridge:main'
    ],
}

setup(name='openprocurement.bridge.contracting',
      version=version,
      description="",
      long_description=open("README.rst").read(),
      classifiers=[
        "Framework :: Pylons",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python",
        "Topic :: Internet :: WWW/HTTP",
        "Topic :: Internet :: WWW/HTTP :: WSGI :: Application"
        ],
      keywords="web services",
      author='Quintagroup, Ltd.',
      author_email='info@quintagroup.com',
      license='Apache License 2.0',
      url='https://github.com/openprocurement/openprocurement.bridge.contracting',
      packages=find_packages(exclude=['ez_setup']),
      namespace_packages=['openprocurement', 'openprocurement.bridge'],
      include_package_data=True,
      zip_safe=False,
      install_requires=requires,
      extras_require={'test': test_requires, 'docs': docs_requires},
      test_suite="openprocurement.bridge.contracting.tests.main.suite",
      entry_points=entry_points,
      )
