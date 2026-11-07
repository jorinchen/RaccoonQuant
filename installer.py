# -*- coding: utf-8 -*-
"""
小浣熊量化选股 - 安装向导

让用户自定义选择安装位置（默认 D:\\Program Files\\RaccoonQuant\\Stock），
将主程序 RaccoonQuant.exe 拷贝到所选目录并创建 data 数据目录。
安装完成后数据（股票列表等）统一保存在 <安装目录>\\data 下。

打包：PyInstaller --onefile --windowed installer.spec -> RaccoonQuant-Setup.exe
"""
import os
import shutil
import subprocess
import sys

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton,
    QHBoxLayout, QVBoxLayout, QFileDialog, QMessageBox, QProgressBar,
)

APP_NAME = '小浣熊量化选股'


def _resource_path(name):
    """读取随安装器打包的附带文件（sys._MEIPASS / 源码目录）"""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _app_version():
    """读取版本号：VERSION 始终位于安装器 _MEIPASS 根（与主程序解耦）。"""
    try:
        with open(_resource_path('VERSION'), 'r', encoding='utf-8') as f:
            v = f.read().strip()
            return v if v.startswith('v') else 'v' + v
    except Exception:
        return 'v1.0.0'


def _append_subdir(base, sub):
    """在基础路径下追加子目录。

    Windows 盘符根目录（如 'D:'）用 os.path.join 会丢斜杠
    （os.path.join('D:', 'x') -> 'D:x'），这里统一补上。
    基础路径以反斜杠结尾（如 'D:\\'）时也规范化处理。
    """
    base = str(base or '').strip().rstrip('\\/')
    if not base:
        return sub
    if base == os.path.splitdrive(base)[0]:  # 形如 'D:' 的盘符根
        return base + os.sep + sub
    return os.path.join(base, sub)


def _default_install_dir():
    """默认安装位置：优先 D 盘，其次其他非系统分区，最后用户目录"""
    for letter in 'DEFGHIJK':
        root = f'{letter}:'
        if os.path.exists(root):
            return _append_subdir(root, 'Program Files\\RaccoonQuant\\Stock')
    return _append_subdir(os.path.expanduser('~'), 'RaccoonQuant\\Stock')


class InstallThread(QThread):
    """后台安装线程：拷贝 exe + 创建数据目录"""
    progress = pyqtSignal(str, int)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, target_dir, parent=None):
        super().__init__(parent)
        self.target_dir = target_dir

    def run(self):
        try:
            self.progress.emit('正在准备安装文件...', 5)
            os.makedirs(self.target_dir, exist_ok=True)

            # 优先尝试新版 onedir 目录结构：主程序整套文件位于 RaccoonQuant\\_internal 下
            src_root = _resource_path('RaccoonQuant')
            if os.path.isdir(src_root) and os.path.exists(
                    os.path.join(src_root, 'RaccoonQuant.exe')):
                target_exe = self._install_dir_tree(src_root)
            else:
                # 兼容旧版 onefile 安装包：仅一个 exe
                src_exe = _resource_path('RaccoonQuant.exe')
                if not os.path.exists(src_exe):
                    self.failed.emit('安装包损坏：未找到 RaccoonQuant.exe，请重新下载安装包。')
                    return
                self.progress.emit(
                    f'正在复制主程序到 {self.target_dir} ...', 50)
                target_exe = os.path.join(self.target_dir, 'RaccoonQuant.exe')
                shutil.copy2(src_exe, target_exe)

            data_dir = os.path.join(self.target_dir, 'data')
            self.progress.emit('正在创建数据目录...', 80)
            os.makedirs(data_dir, exist_ok=True)

            # 复制卸载程序（如安装包内提供）
            target_uninstaller = os.path.join(self.target_dir, 'uninstall.exe')
            src_uninstaller = _resource_path('uninstall.exe')
            if os.path.exists(src_uninstaller):
                self.progress.emit('正在复制卸载程序...', 87)
                shutil.copy2(src_uninstaller, target_uninstaller)
            else:
                target_uninstaller = None

            # 安装完成后在桌面创建主程序快捷方式
            self.progress.emit('正在创建桌面快捷方式...', 93)
            self._create_desktop_shortcut(target_exe)

            self.progress.emit('安装完成', 100)
            self.finished_ok.emit(self.target_dir)
        except Exception as e:
            self.failed.emit(f'安装失败：{e}')

    def _install_dir_tree(self, src_root):
        """递归复制 onedir 主程序目录（RaccoonQuant.exe + _internal 组件）到安装目录。

        边遍历边按文件复制，并按进度发送信号。
        """
        files = []
        for cur, _dirs, names in os.walk(src_root):
            rel = os.path.relpath(cur, src_root)
            for n in names:
                s = os.path.join(cur, n)
                if rel == '.':
                    d = os.path.join(self.target_dir, n)
                else:
                    d = os.path.join(self.target_dir, rel, n)
                files.append((s, d))

        total = max(len(files), 1)
        for i, (s, d) in enumerate(files):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)
            pct = 8 + int(78 * (i + 1) / total)
            self.progress.emit(
                f'正在复制主程序文件（{i + 1}/{total}）...', pct)
        return os.path.join(self.target_dir, 'RaccoonQuant.exe')

    def _create_desktop_shortcut(self, target_exe):
        """在桌面创建主程序快捷方式（卸载程序放在安装目录，不在桌面创建）。"""
        desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
        if not os.path.isdir(desktop):
            desktop = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop')
        workdir = os.path.dirname(target_exe)
        lnk = os.path.join(desktop, APP_NAME + '.lnk')
        ps_cmds = [
            "$ws = New-Object -ComObject WScript.Shell;",
            f"$s = $ws.CreateShortcut('{lnk}');",
            f"$s.TargetPath = '{target_exe}';",
            f"$s.WorkingDirectory = '{workdir}';",
            f"$s.IconLocation = '{target_exe},0';",
            "$s.Save();",
        ]
        ps = ' '.join(ps_cmds)
        try:
            subprocess.run(
                ['powershell', '-NoProfile', '-Command', ps],
                check=False, capture_output=True, timeout=30)
        except Exception:
            pass


class InstallerWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.thread = None
        self.setWindowTitle(f'{APP_NAME} - 安装向导')
        self.setFixedSize(560, 340)
        ico = _resource_path('raccoon.png')
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        title = QLabel(f'{APP_NAME}  {_app_version()}')
        title.setStyleSheet('font-size: 20px; font-weight: bold; color: #1a73e8;')
        layout.addWidget(title)

        desc = QLabel(
            '本程序以"绿色安装"方式安装到您选择的目录（建议非系统分区，\n'
            '如 D:\\Program Files\\RaccoonQuant\\Stock）。股票列表等数据将保存在安装目录的 data 文件夹中。')
        desc.setWordWrap(True)
        desc.setStyleSheet('color: #555;')
        layout.addWidget(desc)

        layout.addSpacing(6)
        layout.addWidget(QLabel('安装位置：'))

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(_default_install_dir())
        self.path_edit.setPlaceholderText('请选择安装目录')
        browse_btn = QPushButton('浏览...')
        browse_btn.clicked.connect(self._browse)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        tip = QLabel('提示：若目标目录已安装旧版本，将直接覆盖升级。')
        tip.setStyleSheet('color: #999; font-size: 12px;')
        layout.addWidget(tip)

        layout.addSpacing(6)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel('就绪')
        self.status_label.setStyleSheet('color: #666;')
        layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.install_btn = QPushButton('开始安装')
        self.install_btn.setStyleSheet(
            'QPushButton { background: #1a73e8; color: white; padding: 8px 28px;'
            ' font-size: 15px; border-radius: 4px; }'
            'QPushButton:hover { background: #1765cc; }')
        self.install_btn.clicked.connect(self._start_install)
        btn_row.addWidget(self.install_btn)
        layout.addLayout(btn_row)

    def _browse(self):
        start = self.path_edit.text().strip() or _default_install_dir()
        d = QFileDialog.getExistingDirectory(self, '选择安装目录', start)
        if not d:
            return
        # 已选择安装目录本身（目录名即 RaccoonQuant 或 Stock）时不再重复追加
        base = os.path.basename(d.rstrip('\\/')).lower()
        if base == 'stock':
            target = d
        elif base == 'raccoonquant':
            target = _append_subdir(d, 'Stock')
        else:
            target = _append_subdir(d, 'RaccoonQuant\\Stock')
        self.path_edit.setText(target)

    def _start_install(self):
        target = self.path_edit.text().strip().rstrip('\\/')
        if not target:
            QMessageBox.warning(self, '提示', '请先选择安装目录。')
            return
        if not os.path.isabs(target):
            QMessageBox.warning(self, '提示', '安装位置必须是绝对路径，如 D:\\Program Files\\RaccoonQuant\\Stock。')
            return
        if self.thread and self.thread.isRunning():
            return

        self.install_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.thread = InstallThread(target)
        self.thread.progress.connect(lambda msg, p: (
            self.status_label.setText(msg), self.progress_bar.setValue(p)))
        self.thread.finished_ok.connect(self._on_done)
        self.thread.failed.connect(self._on_fail)
        self.thread.start()

    def _on_done(self, target_dir):
        self.install_btn.setEnabled(True)
        QMessageBox.information(
            self, '安装完成',
            f'{APP_NAME} {_app_version()} 已安装到：\n{target_dir}\n\n'
            f'数据目录：{os.path.join(target_dir, "data")}\n'
            '现在即可双击 RaccoonQuant.exe 启动使用。')

    def _on_fail(self, msg):
        self.install_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.status_label.setText('安装失败')
        QMessageBox.critical(self, '安装失败', msg)


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    f = QFont('Microsoft YaHei UI', 10)
    app.setFont(f)
    w = InstallerWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
