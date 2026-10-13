# CONTEXT.md（本机部署占位）

上游仓库的 Dockerfile / .dockerignore 引用了 `CONTEXT.md`（术语表），但该文件
未随 git 提交发布，导致 `docker compose build` 在 `COPY run.py pytest.ini CONTEXT.md ./`
一步报 `failed to calculate checksum ... "/CONTEXT.md": not found`。

本部署创建这个最小占位让构建可跑；上游补上真文件后直接覆盖即可。
容器内没有任何代码读取它，仅作为文档随镜像携带。
