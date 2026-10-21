# GitHub部署包核查

仓库：https://github.com/BruceChopZhang/xiangtan-job-radar

## 上传内容

保留：radar/、tests/、scripts/、.github/workflows/radar.yml、sources.json、README.md、VALIDATION.md、DEPLOYMENT.md、LICENSE、run.bat、.gitignore。

排除：本地data数据库和运行日志、site旧仪表盘、manual-run.log、verification原始网页快照、Python缓存、私人条件文件。ZIP内没有这些文件；不要用旧项目的整个目录覆盖本包。

运行必需的SQLite数据库由云端首次运行创建，连同logs.json提交到独立data分支。后续每轮先恢复再增量保存。主分支只放源码；Pages只发布site目录。云端不保存原始网页快照。误删data分支会丢失历史连续性；请勿删除。

## 当前验证

19项测试通过：包括解析、去重、失败保留旧附件、累积日志，以及使用本地Git远端模拟两个独立运行环境的恢复和提交。测试包含远端访问失败时停止、历史数据库缺失时停止。完整原始快照测试已替换为模拟页面测试。

没有在用户GitHub账号执行工作流，没有发布网页，也没有启用定时。GitHub网络、权限及官方来源可达性必须部署后验证；本地模拟不能证明云端成功。

## 零基础操作路线（按指导逐步进行）

1. 下载并解压本包。
2. 上传项目根目录内的文件和文件夹到仓库主分支，特别确认.github/workflows/radar.yml也存在。不要上传ZIP或包含根目录的外层文件夹。Windows若隐藏.github，请打开显示隐藏项目。
3. Settings → Pages → Source选择GitHub Actions。
4. Actions → 湘潭招聘雷达 → Run workflow。工作流只有手动入口；上传不会自动抓取。
5. 检查“测试”“恢复历史”“持久保存”“发布网页”结果。官方来源抓取失败时，任务最终标红，但应保留错误记录；保存或发布失败时网页可能没有更新。
6. 再手动运行一次，确认data分支历史提交延续、旧公告的首次发现时间保留、日志累积，再考虑启用定时。当前没有schedule配置。

## 失败和恢复

历史分支查询、下载或数据库检查失败会停止，不覆盖历史。非快进推送被拒绝时不强推；应检查写入权限和并发修改。保存失败时工作流尝试上传30天临时备份，不能将它当长期存储。网页发布失败不影响已经成功提交到data分支的历史。

超时、取消或程序崩溃不保证完成所有保存；可查Actions日志。手动执行第二次运行前，先确认上一轮已结束。监测日志显示的是实际抓取状态，HTTP 200不代表解析必然成功，公告数不代表可报岗位数。
