# -*- coding: utf-8 -*-
"""
小浣熊量化选股 - 卸载程序

独立 GUI 程序，可选择两种卸载模式：
  - 仅卸载主程序（保留 <安装目录>\\data、<安装目录>\\log）
  - 完全卸载（删除整个安装目录）

打包：PyInstaller --onefile --windowed uninstaller.spec -> RaccoonQuant-Uninstall.exe
安装目录为 <本 exe 所在目录>。
"""
import os
import shutil
import subprocess
import sys

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QRadioButton, QPushButton,
    QVBoxLayout, QHBoxLayout, QButtonGroup, QProgressBar, QMessageBox,
)

APP_NAME = '小浣熊量化选股'


def _resource_path(name):
    """读取随卸载程序打包的附带文件（sys._MEIPASS / 源码目录）"""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def _app_version():
    try:
        with open(_resource_path('VERSION'), 'r', encoding='utf-8') as f:
            v = f.read().strip()
            return v if v.startswith('v') else 'v' + v
    except Exception:
        return 'v1.0.0'


def detect_install_dir():
    """检测安装目录：与本 exe 同目录（安装器将本 exe 部署到 <安装目录>）。"""
    return os.path.dirname(sys.executable)


class UninstallThread(QThread):
    """后台卸载线程"""
    progress = pyqtSignal(str, int)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, install_dir, remove_data, parent=None):
        super().__init__(parent)
        self.install_dir = install_dir
        self.remove_data = remove_data

    def run(self):
        try:
            if not os.path.isdir(self.install_dir):
                self.failed.emit(f'安装目录不存在：{self.install_dir}')
                return

            self.progress.emit('正在准备卸载...', 5)

            # 1. 删除桌面快捷方式
            self._remove_shortcuts()
            self.progress.emit('已移除桌面快捷方式', 12)

            if self.remove_data:
                # 完全卸载：递归删除整个安装目录
                self._rm_tree(self.install_dir, 20, 95)
                self.progress.emit('已删除程序与所有数据', 100)
            else:
                # 仅卸载主程序：保留 data/、log/，删除其余文件/目录
                for name in os.listdir(self.install_dir):
                    if name.lower() in ('data', 'log'):
                        continue
                    p = os.path.join(self.install_dir, name)
                    # 不递归删除自身所在目录（保留逻辑统一交给延迟脚本）
                    if os.path.abspath(p) == os.path.abspath(sys.executable):
                        continue
                    try:
                        if os.path.isdir(p):
                            shutil.rmtree(p)
                        elif os.path.isfile(p):
                            os.remove(p)
                    except Exception:
                        pass
                self.progress.emit('已移除主程序文件（保留数据与日志）', 95)

            # 调度延迟脚本：进程退出后自动删除 RaccoonQuant-Uninstall.exe 自身
            # （Windows 不允许删除正在运行的 exe，故由独立 .bat 在约 3 秒后删除）
            self._schedule_self_delete()
            self.finished_ok.emit(self.install_dir)
        except Exception as e:
            self.failed.emit(f'卸载失败：{e}')

    def _schedule_self_delete(self):
        """通过 wscript 宿主调用 .vbs，再用 vbs 以隐藏窗口方式执行 .bat。

        cmd /c <bat> 会在 Windows 控制台宿主下派生可见窗口；
        PowerShell Start-Process -WindowStyle Hidden 在某些版本仍会闪一下。
        最稳妥的方案：wscript.exe 本身不弹窗，.vbs 用 WshShell.Run 第二个参数 0
        （隐藏）调起 .bat，整个过程完全不显示任何窗口。
        """
        exe_path = sys.executable
        if not os.path.isfile(exe_path):
            return
        try:
            bat_dir = os.environ.get('TEMP', os.path.dirname(exe_path))
            bat_path = os.path.join(bat_dir, f'rq_uninst_{os.getpid()}.bat')
            vbs_path = os.path.join(bat_dir, f'rq_uninst_{os.getpid()}.vbs')

            with open(bat_path, 'w', encoding='gbk') as f:
                f.write('@echo off\r\n')
                # 进程退出到 .bat 启动有 ~3 秒间隔足够（QApplication.quit 已触发）
                f.write('ping -n 4 127.0.0.1 > nul\r\n')
                # 循环重试删除：失败时隔 1 秒再试，最多 20 次（约 23 秒总时长）
                f.write(':retry\r\n')
                f.write(f'del /f /q "{exe_path}" > nul 2>&1\r\n')
                f.write(f'if exist "{exe_path}" (\r\n')
                f.write('  ping -n 2 127.0.0.1 > nul\r\n')
                f.write('  goto :retry\r\n')
                f.write(')\r\n')
                if self.remove_data:
                    f.write(f'rmdir /s /q "{self.install_dir}"\r\n')
                f.write(f'del /f /q "{bat_path}"\r\n')

            # .vbs：用 WScript.Shell 以隐藏窗口方式（intWindowStyle=0）启动 .bat
            with open(vbs_path, 'w', encoding='gbk') as f:
                f.write(
                    'Dim Wsh: Set Wsh = CreateObject("WScript.Shell")\r\n'
                    f'Wsh.Run "cmd /c ""{bat_path}""", 0, True\r\n'
                    'Set Wsh = Nothing\r\n'
                )

            # wscript.exe 本身是无窗口的 Windows 脚本宿主，作为调度者非常稳定
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                ['wscript.exe', vbs_path],
                creationflags=CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception:
            # 调度失败不影响主卸载流程，用户可手动删除
            pass

    def _rm_tree(self, root, start_pct, end_pct):
        """递归删除目录树，按文件/目录数汇报进度"""
        items = []
        # 自底向上删除：先文件后目录
        for cur, dirs, files in os.walk(root, topdown=False):
            for n in files:
                items.append(os.path.join(cur, n))
            for d in dirs:
                items.append(os.path.join(cur, d))
        items.append(root)
        total = max(len(items), 1)
        span = end_pct - start_pct
        for i, p in enumerate(items):
            try:
                if os.path.isdir(p):
                    os.rmdir(p)
                elif os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
            pct = start_pct + int(span * (i + 1) / total)
            if (i + 1) % 20 == 0 or i == total - 1:
                self.progress.emit(f'正在清理文件（{i + 1}/{total}）...', pct)

    def _remove_shortcuts(self):
        desktop = os.path.join(os.path.expanduser('~'), 'Desktop')
        if not os.path.isdir(desktop):
            desktop = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop')
        for lnk_name in (APP_NAME, f'卸载{APP_NAME}'):
            lnk = os.path.join(desktop, f'{lnk_name}.lnk')
            try:
                if os.path.exists(lnk):
                    os.remove(lnk)
            except Exception:
                pass


class UninstallerWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.thread = None
        self.setWindowTitle(f'{APP_NAME} - 卸载程序')
        self.setFixedSize(520, 340)
        ico = _resource_path('raccoon.png')
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        title = QLabel(f'{APP_NAME}  卸载程序  {_app_version()}')
        title.setStyleSheet('font-size: 18px; font-weight: bold; color: #1a73e8;')
        layout.addWidget(title)

        self.install_label = QLabel(f'安装目录：{detect_install_dir()}')
        self.install_label.setStyleSheet('color: #333; font-size: 13px; padding: 6px 0;')
        self.install_label.setWordWrap(True)
        layout.addWidget(self.install_label)

        layout.addSpacing(4)
        tip = QLabel('请选择卸载模式：')
        tip.setStyleSheet('color: #555;')
        layout.addWidget(tip)

        self.radio_keep = QRadioButton('仅卸载主程序（保留股票数据与日志）')
        self.radio_keep.setChecked(True)
        self.radio_full = QRadioButton('完全卸载（删除程序与所有数据）')
        group = QButtonGroup(self)
        group.addButton(self.radio_keep)
        group.addButton(self.radio_full)
        layout.addWidget(self.radio_keep)
        layout.addWidget(self.radio_full)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        layout.addSpacing(6)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel('就绪')
        self.status_label.setStyleSheet('color: #666;')
        layout.addWidget(self.status_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton('取消')
        self.cancel_btn.clicked.connect(self.close)
        self.uninstall_btn = QPushButton('开始卸载')
        self.uninstall_btn.setStyleSheet(
            'QPushButton { background: #e74c3c; color: white; padding: 8px 24px;'
            ' font-size: 15px; border-radius: 4px; }'
            'QPushButton:hover { background: #c0392b; }'
            'QPushButton:disabled { background: #aaa; }')
        self.uninstall_btn.clicked.connect(self._start)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.uninstall_btn)
        layout.addLayout(btn_row)

    def _start(self):
        if self.thread and self.thread.isRunning():
            return
        target = detect_install_dir()
        if self.radio_full.isChecked():
            msg = f'即将完全卸载 {APP_NAME}，\n将删除目录：\n{target}\n\n是否继续？'
        else:
            msg = f'即将仅卸载 {APP_NAME} 主程序，\n将保留目录：\n{target}\\data 与 {target}\\log\n\n是否继续？'
        confirm = QMessageBox.question(
            self, '确认卸载', msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.uninstall_btn.setEnabled(False)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.thread = UninstallThread(target, self.radio_full.isChecked())
        self.thread.progress.connect(self._on_progress)
        self.thread.finished_ok.connect(self._on_done)
        self.thread.failed.connect(self._on_fail)
        self.thread.start()

    def _on_progress(self, msg, pct):
        self.status_label.setText(msg)
        self.progress_bar.setValue(pct)

    def _on_done(self, target_dir):
        QMessageBox.information(
            self, '卸载完成',
            f'{APP_NAME} 已从\n{target_dir}\n成功卸载。\n\n'
            '（卸载程序将在几秒内自动删除）')
        # 弹窗关闭后立即强制退出 app.exec()，让卸载程序进程真正结束，
        # 之后自删除 .bat 才能成功删除 uninstall.exe。
        QApplication.quit()

    def _on_fail(self, msg):
        self.uninstall_btn.setEnabled(True)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.status_label.setText('卸载失败')
        QMessageBox.critical(self, '卸载失败', msg)


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    f = QFont('Microsoft YaHei UI', 10)
    app.setFont(f)
    w = UninstallerWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()