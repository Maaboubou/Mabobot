# Third-party notices

Mabobot includes a vendored copy of the following third-party project:

## Python-UIAutomation-for-Windows

- Location: `mabowx/_vendor/uiautomation/`
- Upstream: <https://github.com/yinkaisheng/Python-UIAutomation-for-Windows>
- Vendored version: 2.0.29
- Copyright: Yinkaisheng and contributors
- License: Apache License 2.0; see [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt)

Other dependencies installed from `requirements*.txt` remain subject to their own licenses and are not relicensed by Mabobot's MIT License.

## Banned Books — Open Censorship Core

- Location: `app/plugins/ebook_downloader/download_policy_rules.json`
- Source dataset: Banned Books — Open Censorship Core, version 2026-07-07
- Source: <https://doi.org/10.5281/zenodo.21235503>
- License: CC BY 4.0, <https://creativecommons.org/licenses/by/4.0/>
- Changes: selected CN active banned/restricted records, converted to plugin rules, with additional Chinese title aliases maintained by this project.
- The dataset attribution and source metadata are also retained in the rule file and plugin README.
