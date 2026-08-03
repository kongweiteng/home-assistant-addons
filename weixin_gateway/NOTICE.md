# 第三方协议参考说明

本 Add-on 的 iLink 请求字段、长轮询、`context_token` 和媒体格式参考了 NousResearch `hermes-agent` 仓库中 MIT 许可的 `gateway/platforms/weixin.py`：

- 固定参考提交：`d0b87dad77944c669b453385bb797d53fa33c4f7`
- 上游项目：https://github.com/NousResearch/hermes-agent
- 上游许可证：MIT
- 上游默认 CDN：`https://novac2c.cdn.weixin.qq.com/c2c`
- 上游媒体域名 allowlist：`novac2c.cdn.weixin.qq.com`、`ilinkai.weixin.qq.com`、`wx.qlogo.cn`、`thirdwx.qlogo.cn`、`res.wx.qq.com`、`mmbiz.qpic.cn`、`mmbiz.qlogo.cn`
- 上游上传行为：优先使用 `upload_full_url`，缺失时使用 `upload_param` 和 `filekey` 拼接默认 CDN 上传 URL

本目录没有运行或打包完整 Hermes；实现只保留 Weixin/iLink 通讯所需的最小协议边界。当前实现沿用上述固定域名集合，但比参考实现更严格：所有 iLink 与 CDN 地址均只接受 HTTPS，HTTP、用户信息、非 allowlist 域名及不符合边界的 URL 均 fail-closed 拒绝。
