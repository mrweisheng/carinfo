#!/bin/bash
# 
# CarInfo Docker部署脚本
# 适用于Ubuntu 20.04服务器
#

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 打印函数
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查系统
check_system() {
    print_info "检查系统环境..."
    
    if [ ! -f /etc/lsb-release ]; then
        print_error "不支持的操作系统"
        exit 1
    fi
    
    . /etc/lsb-release
    if [ "$DISTRIB_ID" != "Ubuntu" ]; then
        print_error "仅支持Ubuntu系统"
        exit 1
    fi
    
    print_success "系统检查通过: $DISTRIB_DESCRIPTION"
}

# 安装Docker
install_docker() {
    print_info "检查Docker安装状态..."
    
    if command -v docker &> /dev/null; then
        print_success "Docker已安装: $(docker --version)"
        return 0
    fi
    
    print_info "安装Docker..."
    
    # 更新包索引
    sudo apt-get update
    
    # 安装必要的包
    sudo apt-get install -y \
        apt-transport-https \
        ca-certificates \
        curl \
        gnupg \
        lsb-release
    
    # 添加Docker官方GPG密钥
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg
    
    # 设置稳定版仓库
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    
    # 更新包索引
    sudo apt-get update
    
    # 安装Docker Engine
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io
    
    # 启动Docker服务
    sudo systemctl start docker
    sudo systemctl enable docker
    
    # 将当前用户添加到docker组
    sudo usermod -aG docker $USER
    
    print_success "Docker安装完成"
    print_warning "请注销并重新登录以使docker组权限生效"
}

# 安装Docker Compose
install_docker_compose() {
    print_info "检查Docker Compose安装状态..."
    
    if command -v docker-compose &> /dev/null; then
        print_success "Docker Compose已安装: $(docker-compose --version)"
        return 0
    fi
    
    print_info "安装Docker Compose..."
    
    # 下载Docker Compose
    sudo curl -L "https://github.com/docker/compose/releases/download/v2.20.2/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    
    # 设置可执行权限
    sudo chmod +x /usr/local/bin/docker-compose
    
    # 创建软链接
    sudo ln -sf /usr/local/bin/docker-compose /usr/bin/docker-compose
    
    print_success "Docker Compose安装完成: $(docker-compose --version)"
}

# 检查配置文件
check_config() {
    print_info "检查配置文件..."
    
    if [ ! -f "config.json" ]; then
        print_warning "config.json不存在，创建默认配置..."
        cat > config.json << EOF
{
    "scraping": {
        "vehicle_types": {
            "1": {"name": "私家车", "enabled": true, "pages": 1},
            "2": {"name": "客货车", "enabled": false, "pages": 0},
            "3": {"name": "货车", "enabled": false, "pages": 0},
            "4": {"name": "电单车", "enabled": false, "pages": 0},
            "5": {"name": "经典车", "enabled": false, "pages": 0}
        }
    }
}
EOF
    fi
    
    if [ ! -f "proxy_config.json" ]; then
        print_warning "proxy_config.json不存在，创建默认配置..."
        cat > proxy_config.json << EOF
{
    "proxies": [],
    "retry_settings": {
        "max_retries": 3,
        "retry_delay": 5,
        "timeout": 30
    },
    "fallback_to_direct": true
}
EOF
    fi
    
    print_success "配置文件检查完成"
}

# 创建必要目录
create_directories() {
    print_info "创建必要目录..."
    
    mkdir -p data logs debug_pages csv_output
    chmod 755 data logs debug_pages csv_output
    
    print_success "目录创建完成"
}

# 构建Docker镜像
build_image() {
    print_info "构建Docker镜像..."
    
    if [ ! -f "Dockerfile" ]; then
        print_error "Dockerfile不存在"
        exit 1
    fi
    
    docker build -t carinfo:latest .
    
    print_success "Docker镜像构建完成"
}

# 测试数据库连接
test_database() {
    print_info "测试数据库连接..."
    
    docker run --rm --add-host=host.docker.internal:host-gateway carinfo:latest python -c "
import mysql.connector
import os
try:
    conn = mysql.connector.connect(
        host='host.docker.internal',
        user='root',
        password='1qaz!QAZ2wsx@WSX',
        database='car_info_db',
        port=3306,
        connect_timeout=10
    )
    conn.close()
    print('数据库连接成功')
except Exception as e:
    print(f'数据库连接失败: {e}')
    exit(1)
"
    
    print_success "数据库连接测试通过"
}

# 部署应用
deploy_app() {
    print_info "部署应用..."
    
    # 停止现有容器
    print_info "停止现有容器..."
    docker-compose down --remove-orphans || true
    
    # 启动服务
    print_info "启动服务..."
    docker-compose up -d
    
    # 等待服务启动
    sleep 5
    
    # 检查服务状态
    print_info "检查服务状态..."
    docker-compose ps
    
    print_success "应用部署完成"
}

# 显示使用说明
show_usage() {
    echo
    print_info "部署完成！使用说明："
    echo
    echo "1. 查看服务状态:"
    echo "   docker-compose ps"
    echo
    echo "2. 查看日志:"
    echo "   docker-compose logs -f carinfo-scraper"
    echo
    echo "3. 停止服务:"
    echo "   docker-compose down"
    echo
    echo "4. 重启服务:"
    echo "   docker-compose restart"
    echo
    echo "5. 单次运行爬取:"
    echo "   docker-compose run --rm carinfo-scraper python carinfo.py --config"
    echo
    echo "6. 启动调度器服务:"
    echo "   docker-compose --profile scheduler up -d"
    echo
    echo "7. 启动Web监控服务:"
    echo "   docker-compose --profile webhook up -d"
    echo
    print_info "配置文件位置："
    echo "- config.json: 爬取配置"
    echo "- proxy_config.json: 代理配置"
    echo "- docker-compose.yml: 容器配置"
    echo
    print_info "数据目录："
    echo "- ./data: 应用数据"
    echo "- ./logs: 日志文件"
    echo "- ./csv_output: CSV输出文件"
    echo "- ./debug_pages: 调试页面"
}

# 主函数
main() {
    echo
    print_info "CarInfo Docker部署脚本"
    echo "========================================"
    
    check_system
    install_docker
    install_docker_compose
    check_config
    create_directories
    build_image
    test_database
    deploy_app
    show_usage
    
    echo
    print_success "部署完成！"
}

# 检查参数
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    echo "使用方法: $0 [选项]"
    echo
    echo "选项:"
    echo "  --help, -h    显示此帮助信息"
    echo "  --build-only  仅构建镜像，不部署"
    echo "  --deploy-only 仅部署，不构建镜像"
    echo
    exit 0
fi

if [ "$1" = "--build-only" ]; then
    check_system
    check_config
    build_image
    print_success "镜像构建完成！"
    exit 0
fi

if [ "$1" = "--deploy-only" ]; then
    check_config
    create_directories
    test_database
    deploy_app
    show_usage
    print_success "部署完成！"
    exit 0
fi

# 执行主函数
main
