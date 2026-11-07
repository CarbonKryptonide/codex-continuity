# 发布到 GitHub

源码已经与私人备份分开。只发布源码文件，不发布会话、记忆、环境、快照或 `.git` 文件夹。

## 第一次发布：直接用网页

不需要安装 Git，不需要 CMD 或 PowerShell，也不需要执行 `git status`。

1. 登录自己的 GitHub 账号，打开 <https://github.com/new>。
2. Owner 选择自己的账号，Repository name 填 `codex-continuity`。公开分享选择 Public。
3. 不另外添加 README、.gitignore 或 License，因为源码包内已经包含这三个文件。
4. 点击 Create repository。在空仓库页点击 uploading an existing file；如果已有文件列表，则选择 Add file → Upload files。
5. 解压源码 ZIP，进入解压后的 `codex-continuity` 文件夹。选择里面的全部文件和文件夹拖到上传区域，保持 `.github`、templates、tests、examples 的目录结构。
6. 上传列表中应有根目录的 README.md、LICENSE、continuity.py、install.py、INSTALL.ps1 等，共17个文件。不要把整个 ZIP 当成源代码上传，也不要多包一层 codex-continuity 文件夹。
7. Commit message 填 `Initial release`，点击 Commit changes。
8. 仓库根目录应直接显示 README.md、continuity.py、INSTALL.ps1 等，页面下方显示项目说明。Actions 工作流会在推送触发后运行；查看结果再判断 CI 是否通过。

官方说明：[新建仓库](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)、[网页上传文件或文件夹](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository)。

## 已经配置 Git：命令行发布

`git status` 是本地命令，和是否登录 GitHub 无关。先按 [GitHub 官方 Git 配置说明](https://docs.github.com/en/get-started/git-basics/set-up-git) 安装 Git 并配置账号认证，然后重开终端。

- 如果提示“git 不是命令”或“无法识别 git”，当前终端找不到 Git。
- 如果提示 `not a git repository`，当前目录不在 Git 仓库中。
- CMD 切换目录使用 `cd /d "源码目录"`；PowerShell 使用 `Set-Location -LiteralPath '源码目录'`。这里的“源码目录”必须替换为实际路径。

在源码目录中先检查：

```powershell
git --version
git status --short
git diff --cached --stat
```

`A` 表示文件已暂存，属于正常状态。核对只有17个源码/模板/测试/文档文件后，在 GitHub 创建空仓库，再执行下面的命令，把示例地址替换成自己的仓库地址：

```powershell
git add .
git commit -m "Initial release"
git remote add origin https://github.com/YOUR_ACCOUNT/codex-continuity.git
git push -u origin main
```

若 `git commit` 提示缺少身份，在本仓库设置自己的署名和已验证邮箱（或 GitHub 隐私邮箱），然后重新提交。Git 身份与 GitHub 登录认证是两个步骤。

不要在私人备份根目录运行 `git add .`；源码目录的 `.gitignore` 也不会清除已经提交的敏感文件，因此从这个干净仓库开始。
