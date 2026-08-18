"""media 包 — Agnes AI 图片/视频生成客户端与执行层。

子模块：
  - http         带重试的下载与 JSON 请求
  - agnes_image  文生图 / 图生图（agnes-image-2.1-flash）
  - agnes_video  异步视频任务（agnes-video-v2.0）
  - sanitize     图片/视频 prompt 内容安全清洗
  - executor     资产图/镜头视频生成 + 拼接的高层编排
  - ffmpeg       ffmpeg 路径查找与视频拼接
"""
