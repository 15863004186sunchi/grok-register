# 使用 Playwright 官方镜像，包含浏览器和依赖
FROM mcr.microsoft.com/playwright:v1.49.1-jammy

# 设置工作目录
WORKDIR /app

# 安装 Python 相关工具和 xvfb
RUN apt-get update && apt-get install -y \
    python3-pip \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# 拷贝依赖文件并安装
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt \
    && pip3 install --no-cache-dir pyvirtualdisplay

# 拷贝项目文件
COPY . .

# 创建必要的目录
RUN mkdir -p sso logs

# 设置环境变量，强制启用 Xvfb (脚本内逻辑已包含，此处显式指定)
ENV DISPLAY=:99
ENV USE_XVFB=1
ENV PYTHONUNBUFFERED=1

# 启动命令
CMD ["python3", "DrissionPage_example.py"]
