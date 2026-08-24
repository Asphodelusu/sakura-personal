# Acceptance Tests

- V1 读取不改变磁盘。
- V1 下一次真实写入生成 V2，正文与 metadata 保持正确。
- V2 content 和 sections 只读降级均兼容。
- 未知 schema 可在有 content 时只读，但 set/delete 拒绝且字节不变。
- 损坏 JSON 只读返回 None，set/delete 拒绝且字节不变。
- 覆盖写生成 `.bak`，首次新建不生成空备份。
- 重复 V2 写入幂等；旧整段 API 重置为单一 legacy。
- 不读取或修改任何真实数据。

