# python-utility-scripts
Repository for various python utility scripts
* [pyutils-unusedcode](https://github.com/RedHatQE/python-utility-scripts/blob/main/apps/unused_code/README.md)
* [pyutils-polarion-verify-tc-requirements](https://github.com/RedHatQE/python-utility-scripts/blob/main/apps/polarion/README.md)

## Installation

```bash
pip3 install python-utility-scripts --user
```

## Local run
* Clone the [repository](https://github.com/RedHatQE/python-utility-scripts.git)

```bash
git clone https://github.com/RedHatQE/python-utility-scripts.git
```

* Install [poetry](https://github.com/python-poetry/poetry)

```bash
poetry install
```

## Config file
A config yaml file for various utilities of this repository should be added to
`~/.config/python-utility-scripts/config.yaml`. Script specific config section details can be found in associated script README.md


## Release new version
### requirements:
* Export GitHub token

```bash
export GITHUB_TOKEN=<your_github_token>
```

* [release-it](https://github.com/release-it/release-it)

* Run the following once (execute outside repository dir for example `~/`):

```bash
sudo npm install --global release-it
npm install --save-dev @j-ulrich/release-it-regex-bumper
rm -f package.json package-lock.json
```

### usage:
* Create a release, run from the relevant branch.
To create a new release, run:

```bash
git checkout main
git pull
release-it # Follow the instructions
```
