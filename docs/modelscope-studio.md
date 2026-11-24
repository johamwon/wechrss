---
# 详细文档见https://modelscope.cn/docs/%E5%88%9B%E7%A9%BA%E9%97%B4%E5%8D%A1%E7%89%87
domain: #领域：cv/nlp/audio/multi-modal/AutoML
# - cv
tags: #自定义标签
- rss
- wechat
- weixin
- python
- self-hosted
datasets: #关联数据集
  evaluation:
  #- iic/ICDAR13_HCTR_Dataset
  test:
  #- iic/MTWI
  train:
  #- iic/SIBR
models: #关联模型
#- iic/ofa_ocr-recognition_general_base_zh

## 启动文件(若SDK为Gradio/Streamlit，默认为app.py, 若为Static HTML, 默认为index.html)
# deployspec:
#   entry_file: app.py
license: MIT License
---

# WechRSS · 公众号互助订阅

用 RSS 阅读器跟进微信公众号更新。参与者自愿扫码授权并补充公众号，平台共用账号池维护共享目录。已经有人添加的公众号，其他访客可以直接订阅。

[浏览共享订阅目录](https://www.modelscope.cn/studios/johamwon/wechrss?mode=full&app_path=%2Fcommunity) · [查看账号池并参与互助](https://www.modelscope.cn/studios/johamwon/wechrss?mode=full&app_path=%2Faccount-pool)

## 如何参与

- **我想订阅。** 在共享目录找到公众号，复制 RSS 地址，添加到常用阅读器。浏览和订阅无需登录。
- **我想补充公众号。** 用自己的微信扫码授权，回到目录粘贴目标公众号的一篇公开文章链接。平台识别公众号、合并重复提交，并生成共享 RSS。
- **我想撤回授权。** 在原浏览器打开账号池，选择自己的会话并撤回。已添加到共享目录的订阅继续保留。

## 平台如何同步

后台轮换有效的微信读书会话，按计划获取公众号更新。账号池用匿名编号展示有效、待验证和已失效的状态。遇到平台限频或要求验证时，自动同步会暂停，已有 RSS 仍可访问。

Feed 提供文章标题、摘要和原文链接，阅读全文请打开微信原文。扫码会话的登录凭证由服务端保存，公开页只显示匿名状态。这里是共享实例，请只提交愿意公开共享的公众号。

## 项目源码

查看自托管方式、提交问题或参与开发。 [WechRSS GitHub 仓库](https://github.com/johamwon/wechrss)
