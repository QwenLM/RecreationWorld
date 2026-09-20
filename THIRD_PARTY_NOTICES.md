# Third-party notices

This file covers third-party artifacts copied into, or downloaded directly by, the
RecreationBench runtime, as well as selected package-manager dependencies.
Package-manager dependencies are not vendored in this source
tree and retain the licenses published with their respective distributions. Published
environment images should include their own dependency inventory and applicable
license notices.

## Qwen CUA Driver

The macOS runtime downloads `QwenCuaDriver.app` from the public
[Qwen Code](https://github.com/QwenLM/qwen-code) release
`cua-driver-rs-v0.7.3`. The release tag resolves to source commit
`cb483092561a3606dfd53447a26f3718380f0f59` and is licensed under
[Apache-2.0](https://github.com/QwenLM/qwen-code/blob/cb483092561a3606dfd53447a26f3718380f0f59/LICENSE).

RecreationBench pins the release archive URL and SHA-256 in
`scripts/core/cua_driver.py`; the runtime verifies the digest before extracting or executing
the bundle.

## Paramiko

[Paramiko](https://www.paramiko.org/) provides SSH connections and SFTP file transfers
for remote execution and deployment. RecreationBench installs it as a Python dependency;
Paramiko source code is not vendored or modified in this repository.

Paramiko is licensed under the GNU Lesser General Public License, version 2.1 or later
(`LGPL-2.1-or-later`). Its [source code](https://github.com/paramiko/paramiko) and
[full license text](https://github.com/paramiko/paramiko/blob/main/LICENSE) are available
upstream. Copyright belongs to Robey Pointer and other Paramiko contributors, as
recorded in the upstream source files. Distributions that include Paramiko must retain
its applicable copyright and license notices and meet the LGPL's source-code
availability requirements.
