# HA Release typed artifact v2（本地候选）

HARelease 与 HAReleaseRequest schema_version=2；制品闭合为 Docker ImageID
`{kind:docker_image_id,digest:sha256:...}` 或 OCI RepoDigest
`{kind:oci_repo_digest,repository:registry.example.invalid/path,digest:sha256:...}`。
同 SHA 不跨 kind 相等，OCI 还要求 repository 一致；空 RepoDigests 只可匹配 Docker。

独立 source→build 资格冻结期待值；运行 Observer 仅提供绑定 target/instance/slug 的实际
version1 fact，不能反填期待值。观察 Port 缺失时失败关闭。实际 docker_image_id 进入
release-baseline-v2，健康分别核验源码、数据、配置与 typed identity；重建改变 ImageID
需要重新资格与新计划。SQL Delivery5、Broker7、executor journal1 和 AES 格式不变。

历史 schema1 request 启动 recovery 跳过、读取拒绝；历史记录/hash/锁不改。真实
Supervisor/Observer/构建/WebAuthn/NAS 恢复尚待独立验收；当前 main 不接线此执行器。
本地测试为 HTTP/SQLite/AES 和独立文件测量，不能据此宣称正式 HA 已验收。
