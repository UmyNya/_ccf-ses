def format_size(size_list):
    """将文件大小分布格式化为VDBench语法"""
    return "size=(" + ",".join([f"{size},{percent}" for size, percent in size_list]) + ")"

def generate_vdbench_config(config, variables):
    """生成VDBench配置文件内容"""
    lines = []
    
    # 生成host definition
    hd_params = ",".join([f"{k}={v.format(**variables)}" for k, v in config['hd'].items()])
    lines.append(f"hd=default,{hd_params}")
    
    # 生成FSD默认参数
    fsd_default = ",".join([f"{k}={v}" for k, v in config['fsd_default'].items()])
    lines.append(f"fsd=default,{fsd_default}")
    
    # 生成FSD实例
    fsd_tmp = config['fsd_tmp'].copy()
    size_str = format_size(fsd_tmp.pop('size'))
    fsd_params = ",".join([f"{k}={v.format(**variables)}" for k, v in fsd_tmp.items()])
    lines.append(f"tmp=fsd,{fsd_params},{size_str}")
    
    # 生成FWD默认参数
    fwd_default = ",".join([f"{k}={v.format(**variables)}" for k, v in config['fwd_default'].items()])
    lines.append(f"fwd=default,{fwd_default}")
    
    # 生成FWD工作负载
    for fwd in config['fwd_operations']:
        # 处理读写比例拆分
        if isinstance(fwd.get('operation'), list) and isinstance(fwd.get('skew'), (int, float)):
            total_skew = fwd['skew']
            operations = fwd['operation']
            # 验证比例总和是否为100%
            total_percent = sum(pct for _, pct in operations)
            if total_percent != 100:
                raise ValueError(f"操作比例总和必须为100%，当前为{total_percent}%")
            
            for op, pct in operations:
                # 计算实际skew值并四舍五入
                op_skew = round(total_skew * pct / 100)
                # 创建新参数
                params = {
                    'fsd': fwd['fsd'],
                    'operation': op,
                    'xfersize': fwd['xfersize'],
                    'fileio': fwd['fileio'],
                    'fileselect': fwd['fileselect'],
                    'skew': op_skew
                }
                param_str = ",".join([f"{k}={v}" for k, v in params.items()])
                lines.append(f"tmp=fwd,{param_str}")
        else:
            # 直接生成原始配置
            params = ",".join([f"{k}={v}" for k, v in fwd.items()])
            lines.append(f"tmp=fwd,{params}")
    
    # 生成运行定义
    rd_params = ",".join([f"{k}={v.format(**variables)}" for k, v in config['rd'].items()])
    lines.append(f"rd={config['rd_name']},{rd_params}")
    
    return "\n".join(lines)

# 配置参数模板（使用{}包裹变量占位符）
config_template = {
    "hd": {
        "shell": "ssh",
        "vdbench": "{vdbench_dir}",
        "user": "root"
    },
    "fsd_default": {
        "openflags": "o_direct",
        "shared": "yes"
    },
    "fsd_tmp": {
        "depth": "{depth}",
        "width": "{width}",
        "files": "1000",
        "size": [
            ("11K", 35),
            ("36K", 15),
            ("17K", 13),
            ("73K", 13),
            ("56K", 8),
            ("182K", 6),
            ("4K", 10)
        ]
    },
    "fwd_default": {
        "thread": "{thread}"
    },
    "fwd_operations": [
        {
            "fsd": "all",
            "operation": [("read", 80), ("write", 20)],
            "xfersize": "11k",
            "fileio": "sequential",
            "fileselect": "sequential",
            "skew": 45 
        },
        {
            "fsd": "all",
            "operation": [("read", 80), ("write", 20)],
            "xfersize": "36k",
            "fileio": "sequential",
            "fileselect": "sequential",
            "skew": 19  
        },
        {
            "fsd": "all",
            "operation": [("read", 80), ("write", 20)],
            "xfersize": "17k",
            "fileio": "sequential",
            "fileselect": "sequential",
            "skew": 11  
        },
        {
            "fsd": "all",
            "operation": [("read", 80), ("write", 20)],
            "xfersize": "73k",
            "fileio": "sequential",
            "fileselect": "sequential",
            "skew": 10  
        },
        {
            "fsd": "all",
            "operation": [("read", 80), ("write", 20)],
            "xfersize": "56k",
            "fileio": "sequential",
            "fileselect": "sequential",
            "skew": 6  
        },
        {
            "fsd": "all",
            "operation": [("read", 80), ("write", 20)],
            "xfersize": "182k",
            "fileio": "sequential",
            "fileselect": "sequential",
            "skew": 4  
        },
        {
            "fsd": "all",
            "operation": [("read", 80), ("write", 20)],  # 保持单操作类型直接用 "write" 也行
            "xfersize": "4k",
            "fileio": "sequential",
            "fileselect": "sequential",
            "skew": 5  
        }
    ],
    "rd_name": "rd1",
    "rd": {
        "fwd": "(fwd*)",
        "fwdrate": "{fwdrate}",
        "format": "{format}",
        "elapsed": "{elapsed}",
        "interval": "1"
    }
}

# 实际变量值
variables = {
    "depth": "$depth",
    "width": "$width",
    "thread": "$thread",
    "fwdrate": "$fwdrate",
    "format": "$format",
    "elapsed": "$elapsed",
    "vdbench_dir": "$vdbench_dir"
}

# 生成配置文件内容
config_content = generate_vdbench_config(config_template, variables)

# 保存到文件或打印输出
file_name = "photo_temp.txt"
with open(file_name, "w") as f:
    f.write(config_content)

print("VDBench配置文件已生成：",file_name)
print("该配置文件仅供参考，请手动检查和重命名。")