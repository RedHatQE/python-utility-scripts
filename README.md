# python-utility-scripts
Repository for various python utility scripts

## Installation
```bash
pip3 install python-utility-scripts
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

# Scripts
## unusedcode
Helper to identify unused code in a pytest repository. It should be run from inside the test repository using this tool.

### Usage
```bash
poetry run unusedcode
```
### Config file
To skip unused code check on specific files of a repository, a config file with the list of names of such files, separated by `\n` should be added to
`~/.config/python-utility-scripts/config.yaml`

e.g to skip running unsedcode check on file my_exclude_file.py, following can be added to `~/.config/python-utility-scripts/config.yaml`
```bash
my_exclude_file.py
```

## Release new version
### requirements:
* Export GitHub token
```bash
export GITHUB_TOKEN=<your_github_token>
```
* [release-it](https://github.com/release-it/release-it)
Run the following once (execute outside repository dir for example `~/`):
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