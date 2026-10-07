# Langfuse Message Dedup — 实验版

[English](README.md) | 简体中文

可选 Python 扩展：把重复的长消息内容保存为 Langfuse 原生 JSON 附件，
观测记录保留引用，读取时校验 SHA256 并恢复原文。这是非官方社区项目，
独立于 Langfuse 源码；代码已开源，尚未发布到 PyPI。

## 接入

```powershell
git clone https://github.com/lumman0/langfuse-message-dedup.git
cd langfuse-message-dedup
python -m pip install ".[example]"
$env:LANGFUSE_BASE_URL = "http://localhost:3000"
# 在当前进程中设置测试项目的 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY。
python examples/basic.py
```

```python
from langfuse_message_dedup import (
    LangfuseMediaStore, MediaContext, MessageDeduplicator,
)

with LangfuseMediaStore(
    base_url=base_url, public_key=public_key, secret_key=secret_key,
) as store:
    codec = MessageDeduplicator(store, min_bytes=4096)
    result = codec.encode_messages(
        messages,
        context=MediaContext(
            trace_id=generation.trace_id,
            observation_id=generation.id,
            field="input",
        ),
    )
    generation.update(input=result.payload)
    # messages 仍是原始模型输入，result.payload 仅用于观测。
    # 在自己的监控中统计 result.failures，失败时对应值保留原文。
    restored = codec.restore_messages(result.payload)
    assert restored == messages
```

读取已持久化的 input 后，同样调用 `restore_messages(recorded_input)`。
output 如果是一条消息列表，也用同样接口；如果是字符串，可用下面的根路径接口。

## 显式结构化字段

```python
paths = [("history", i, "content") for i in range(len(payload["history"]))]
result = codec.encode_fields(payload, paths=paths, context=context)
original = codec.restore_fields(result.payload, paths=paths)

# 单独处理一个字符串或 JSON 值：根路径为 ()。
result = codec.encode_fields(reply_text, paths=[()], context=output_context)
reply_text = codec.restore_fields(result.payload, paths=[()])
```

不自动猜测 JSON 字符串内部结构；若业务把状态、摘要、历史打包成一个字符串，
应显式构造观测用的结构化副本。恢复保证 JSON 值和字符串内容一致，不保证原始
JSON 文本的空格、键顺序序列化形式等线缆字节一致。

## 行为保证

- 原地输入不变；角色、工具调用、顺序等未选中字段保留。
- 只接受 JSON 类型，拒绝 NaN、tuple、非字符串键和保留键 `$lf_message_dedup`。
- 对 JSON 编解码会改变的非标准 Python Unicode 表示（如字面代理对），保留原值
  内联并记录失败，避免产生还原后内容变化的引用。
- 历史发生编辑时产生新内容，不覆盖旧 trace 的消息。
- 复用以实际序列化的内容为准；JSON 对象键顺序不同可能无法去重。
- 在当前项目内利用媒体服务的内容去重，每个 trace/observation/field 都登记关联。
- 同步 POST → PUT → PATCH 完成后才返回引用。任一步失败时该值保持内联，
  `result.failures` 仅包含字段路径和错误类型，不包含密钥、正文或签名 URL。
- 读取时检查长度、SHA256、版本；丢失或损坏抛 `RestoreError`，不返回假完整结果。
- 默认阈值 4096 字节，可配置；至少 512 字节且替换后的引用确实更短才外置。
  该阈值尚未做生产调优。单个附件默认上限 16 MiB，超过后保持内联。
- 读取缓存仅在一次 restore 内存在；不跨项目、不跨请求缓存正文或签名 URL。

## 已知边界

1. **原生界面兼容有限**：文本/JSON 附件走文件展示，不会无感还原为原聊天内容。
   现有全文搜索无法自动搜索附件正文；内置评测、数据集导出需要单独验证/接入还原。
2. **显式接入**：首版不是自动装配的 OTel exporter，也不拦截任意 SDK 上报。
   自动埋点若在其他 span 又记录完整输入，那些副本仍会占空间；需逐个接入。
3. **脱敏顺序**：先完成业务脱敏，再调用编码器。SDK 最后的 masking 钩子
   此时看到的是引用，无法替附件正文完成脱敏。
4. **同步开销**：每个大消息需要媒体 API 请求（复用也需要登记关联）。放在
   业务自行管理的遥测工作线程中，避免阻塞模型响应；首版不自带后台队列。
   请求超时是单次网络操作限制，不是整段历史的总耗时限制。
5. **存储后端**：初始目标是 S3 兼容对象存储，PUT 成功码 200；MinIO 是实测目标。
   没有宣称支持 Azure/GCS/所有 Cloud 配置。Langfuse 必须先启用媒体存储。
6. **生命周期**：上传后、上报前进程退出可能留下孤立媒体记录；上传失败也可能
   留下待完成记录。不会产生已返回但未确认上传的引用，但不承诺原子事务。
   外部删除、保留期和共享引用回收由服务端决定；首版不提供 GC 或删除操作。
7. **费用**：减少重复正文不等于一定降低总成本。对象请求数、Postgres 媒体关联行、
   索引、WAL、ClickHouse 压缩都要算进去。长且重复的消息是候选场景；短消息和
   高变化历史不适合盲目开启。

## 验证

```powershell
python -m pip install ".[dev,example]"
python -m pytest
python -m ruff check src tests examples scripts
python scripts/live_probe.py --report reports/live.json
```

`tests/` 包含纯逻辑测试和模拟 HTTP 协议测试。它们不能代替真实 Langfuse 验证。
`live_probe.py` 使用真实 SDK/OTLP、媒体 API 和对象存储，生成六轮合成对话：
复用历史、修改历史、重新创建客户端、服务不可达回退、引用缺失、持久化读取还原。
它不调用收费模型，不删除数据；会在指定测试项目留下带 `dedup-probe-` 名称的观测。
报告统计实际上传次数和下载到的正文大小，但不把逻辑字节数称为总磁盘节省。

## 设计资料

- [实施范围](docs/plan.md)
- [真实验证记录](docs/verification.md)
- [Langfuse 媒体接口](https://langfuse.com/docs/observability/features/multi-modality)

这是验证可行性的开发版本；请先用于独立测试项目。

## 许可证与贡献

[MIT](LICENSE)。欢迎通过 issue 讨论问题和优化方案，贡献前请阅读
[CONTRIBUTING.md](CONTRIBUTING.md)。
