---
id: rb-db-pool
title: 数据库连接池耗尽处置手册
symptom: cannot get connection、Too many connections、请求排队
cause: 连接上限过低、事务未及时提交、慢查询长期占用连接
severity: P0
tags: 连接池 数据库 超时 慢查询
---

# 数据库连接池耗尽处置手册

**适用症状**：cannot get connection、Too many connections、请求排队

**常见成因**：连接上限过低、事务未及时提交、慢查询长期占用连接

**处置步骤**：

1. 区分是服务端连接数打满还是应用池打满：Too many connections 属前者
2. 先看慢查询而不是先调大池——调大池只会把排队挪到数据库里
3. 临时措施：下调单实例池上限并加队列超时，避免雪崩扩散
4. 确认应用侧是否有未关闭连接（泄漏表现为池占用只增不减）
5. 为关键接口加熔断与快速失败，retry=0 且无超时的组合必须整改
6. 整改后压测验证 p99 与错误率，写回容量基线

**严重级**：P0
