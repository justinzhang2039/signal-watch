# signal-watch

把"对明年的判断"变成一个自己运行的信息系统：每天自动采集公开信号，只在判断被证实、被推翻、出现现在就能用上的东西、或系统自己坏了的时候，往 RSS 里写一条。平时不用看。

当前主力追踪 Agents 网络（含 AAIF 的项目接纳和工作组动态），空间计算只保留三个低噪音源。判断和数据源都在 `config/signals.yaml` 里，换成你自己的就是你的版本。

## 它怎么工作

| 层 | 做什么 | 花钱吗 |
|---|---|---|
| 采集 | 拉 AAIF 官网和工作组仓库、GitHub 发版、arXiv、IETF 草案、Hacker News、Apple 开发者 RSS、npm / PyPI 下载量 | 不花 |
| 规则 | 关键词筛掉噪音，首次见到的源只建基线 | 不花 |
| 判定 | 只把通过规则的条目交给模型，问"这是否证实或推翻某条判断" | 很少 |
| 发布 | 写 RSS 和一个静态看板页 | 不花 |

判定层主用 [Jev](https://docs.typesafe.ai)（只返回概率，不生成文字），备用 OpenAI。两者都有写死在配置里的每周调用上限，到了就停。

结果分三级：概率 ≥ 0.85 且是实质性事件的进**警报**；0.5 到 0.85 的进**月度摘要**；其余只留在日志里。

## 部署（一次性，约 10 分钟）

1. 在 GitHub 新建一个**公开**仓库，把本目录全部内容推上去：

   ```bash
   cd signal-watch
   git init -b main
   git add -A
   git commit -m "init"
   git remote add origin https://github.com/<你的用户名>/<仓库名>.git
   git push -u origin main
   ```

2. 仓库 Settings → Secrets and variables → Actions：
   - Secrets 里新建 `TYPESAFE_API_KEY`（Jev 密钥）。
   - 想要备用通道的话，Secrets 里新建 `OPENAI_API_KEY`，再到 Variables 里新建 `OPENAI_MODEL`，填你想用的最便宜的模型名。不设就不启用备用。
3. Settings → Pages → Source 选 "Deploy from a branch"，分支 `main`，目录 `/docs`。
4. Actions 页 → watch → Run workflow，手动跑第一次。第一次只建基线，不会有警报。
5. 再运行一次并勾选 `selftest`，它会真实调用一次判定接口来验证密钥，不采集也不写回。

之后每天北京时间 06:17 左右自动运行。订阅地址：

- 警报：`https://<用户名>.github.io/<仓库名>/alerts.xml`
- 月度摘要：`https://<用户名>.github.io/<仓库名>/digest.xml`
- 看板：`https://<用户名>.github.io/<仓库名>/`

## 密钥安全

- 密钥只存在 GitHub 的 Actions Secrets 里，不进代码、不进提交记录，fork 的人拿不到。
- 脚本不打印请求内容；接口报错时只记状态码。公开仓库的运行日志是公开的，所以这一点是写死的。
- 建议在 OpenAI 后台为这个用途单独建一个项目和密钥，并设月度预算，和其他用途隔开。

## 花费

Jev 按输入 token 计费，每百万 token 0.042 美元，输出免费。每条判定约 600 到 900 token，每周上限 200 次，所以一周的上限约 0.008 美元。看板页底部会显示本周实际用量。

## 改成你自己的

只改 `config/signals.yaml`：

- `hypotheses`：你的判断。`confirm` 和 `refute` 是给模型的是非题，写得越具体越准。
- `sources`：数据源。`type: feed` 可以接任何 Atom 或 RSS。
- `thresholds`：嫌警报多就调高 `alert`，嫌少就调低。
- `metrics`：数值指标，偏离基线若干倍就报，不经过模型。

没有任何密钥也能跑：采集和指标照常，候选条目留在待判队列里。

## 本地测试

```bash
pip install pyyaml
python tests/test_pipeline.py
```

测试用假的网络和假的模型跑完整流程，不发任何真实请求。

## 已知限制

- 判定依赖模型概率，会有漏报和误报。看板上的证据条数是材料，不是结论。
- `aaif-wg-security` 和 `aaif-tc` 两个源的分支名没有实测；如果连续失败，警报 RSS 会报一条故障，把网址里的 `main` 改成实际分支即可。
- AAIF 官网条目只有网址里的标题，没有正文，模型判定的依据偏少。
- 中文信息源没有接。
- 工作流里的 Action 版本（checkout@v4、setup-python@v5）过期后需要手动升级，届时运行会失败并由 GitHub 发邮件提醒。

## 目录

```
config/signals.yaml   判断、数据源、阈值
watch/                采集、规则、判定、发布
data/                 运行日志和状态（自动生成并提交）
docs/                 RSS 和看板（GitHub Pages 发布目录）
```
