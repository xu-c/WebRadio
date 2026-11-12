# WebRadio

适配 M5Stack Cardputer Zero 的 320×170 彩屏和内置键盘。电台目录由 [Radio Browser](https://www.radio-browser.info/) 提供，音频由设备上的 mpv 播放。

## 在 Cardputer Zero 上安装

用 `python build_package.py` 生成安装包。把 `dist/cardputer-zero-radio_1.0.1_all.deb` 复制到已联网的 Cardputer Zero，在设备上进入安装包所在目录，执行：

```sh
sudo apt update
sudo apt install --no-install-recommends ./cardputer-zero-radio_1.0.1_all.deb
```

安装后，在 Cardputer Zero 的应用启动器里打开 **WebRadio**。旧版 Radio 可以直接升级，收藏和音量设置会保留。若使用自定义系统镜像，可用 `cardputer-zero-radio --framebuffer /dev/fb0 --keyboard /dev/input/eventX` 指定设备。画面倒置时加 `--rotate`。

## 快捷键

| 按键 | 操作 |
| --- | --- |
| ↑ / ↓ 或 K / J | 选择电台 |
| Enter | 播放选中的电台。对正在播放的电台再按一次可停止，连接失败时可重试 |
| Space | 停止播放。停止后再按会播放当前选中的电台 |
| / | 按台名搜索。Enter 确认，Backspace 删除，Ctrl+U 清空 |
| F | 按国家／地区、语言、类型筛选，并选择排序。方向键选择和切换，E 输入自定义值，X 重置，Enter 应用 |
| S | 收藏或取消收藏选中的电台 |
| Tab 或 1 / 2 | 切换发现和收藏 |
| ← / → 或 , / . | 翻页 |
| − / + | 调节音量，直接按 = 也可增加 |
| R | 刷新当前目录页 |
| H | 查看按键说明 |
| Esc | 关闭当前面板。位于列表时退出应用 |

设备上可直接输入英文台名、频率或呼号，也可以用「中文／普通话／粤语」等语言预设缩小范围。界面和电台名支持中文显示。实体键盘输入目前使用英文字母、数字和符号，桌面预览可以用 Ctrl+V 粘贴中文关键词。

## 连接行为

目录查询在后台进行，通过 HTTPS 访问 Radio Browser。再次打开同一页时会先显示已保存的结果，再后台刷新。请求失败时会尝试其他镜像，全部失败时继续显示缓存。收藏保存在设备上，电台地址可直接使用，播放仍需要网络。应用默认隐藏 Radio Browser 标记为失效的电台。

播放时会缓冲音频，并监控播放进度。断线或长时间停滞时，播放器会尝试备用流地址，并按 1、2、4、8 秒的间隔重连。搜索、筛选和切换收藏页时，当前电台会继续播放。

## 桌面预览与打包

需要 Python 3.9+、Tk 和 Pillow 9.2+。Windows 使用系统中文字体，Linux 可安装 `fonts-noto-cjk`，也可通过 `ZERO_RADIO_FONT` 指定字体路径。

```sh
python -m pip install "Pillow>=9.2"
python radio.py --demo
python build_package.py
```

`--demo` 使用内置示例电台，适合检查搜索、筛选、收藏和界面。省略 `--demo` 会查询真实目录。实际音频播放需要 Linux 和 mpv。
