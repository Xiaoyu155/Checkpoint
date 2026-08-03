"""EXE entry point — launches the desktop workbench directly, no CLI parsing."""
import sys
import os

def main():
    """主入口，带错误处理"""
    try:
        # 确保当前目录正确
        if getattr(sys, 'frozen', False):
            os.chdir(os.path.dirname(sys.executable))

        from visual_agent.workbench_app import launch_desktop_app
        return launch_desktop_app()
    except Exception as e:
        # 显示错误而不是闪退
        _show_error("启动错误", str(e))
        return 1

def _show_error(title, message):
    """显示错误对话框"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        print(f"\n{title}: {message}\n")
        input("按 Enter 键退出...")

if __name__ == "__main__":
    sys.exit(main())
