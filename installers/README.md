# installers —— 离线安装包

这个文件夹放的是“装环境”用的安装程序，目的是让工具在**没有网络**的电脑上也能装起来。

## 内容

| 文件 | 用途 | 大小 |
| --- | --- | --- |
| `python-3.12.10-amd64.exe` | Python 3.12.10 官方 64 位安装包（Windows） | 约 26 MB |
| `wheels\` | 项目全部 Python 依赖的预下载包（cp312 / win_amd64） | 约 11 MB |

安装脚本 `setup.ps1` 会自动使用它们：

1. 电脑上没有 Python 时，先静默运行 `python-3.12.10-amd64.exe`（装到当前用户目录、自动加入 PATH、不需要管理员权限）；
2. 安装依赖时优先执行 `pip install --no-index --find-links installers\wheels`，全程不联网；
3. 只有当 Python 版本不是 3.12（例如你手动装了 3.13）导致 wheel 不匹配时，才会退回联网安装。

## 来源与校验

- Python 安装包来自官方：<https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe>
- wheels 由本机 `pip download -r requirements.txt --only-binary=:all:` 生成，与 `requirements.txt` 中锁定的版本一致。

想自行校验 Python 安装包，可在 PowerShell 中执行：

```powershell
Get-FileHash .\installers\python-3.12.10-amd64.exe -Algorithm SHA256
```

官方 SHA256 见 <https://www.python.org/downloads/release/python-31210/> 页面底部的 “Windows installer (64-bit)” 表格。

## 说明

- 这些文件只是安装素材，不会被程序运行时代码引用，删掉也不影响已经装好的环境；
- `wheels` 里的包与 Python 版本、操作系统绑定（cp312 / win_amd64），换系统或换 Python 版本需要重新生成；
- 升级依赖版本后，请重新执行一次 `pip download` 更新 `wheels`，否则离线安装会装上旧版本。
