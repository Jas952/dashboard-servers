#!/usr/bin/env python3
import json
import os
import sys
import time
import select
import termios
import tty
import shutil
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

LOG_FILE = os.path.join(ROOT_DIR, "data", "global_logs.jsonl")

def load_logs():
    logs = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try: logs.append(json.loads(line))
                    except: pass
        except: pass
    return logs

def save_logs(logs):
    try:
        with open(LOG_FILE, "w") as f:
            for log in logs:
                f.write(json.dumps(log) + "\n")
    except: pass

def main():
    selected_idx = 0     # 0-3 for top buttons, 4+ for logs
    selected_btn = 0     # for logs: 0 = COPY, 1 = DELETE
    scroll_offset = 0
    running = True

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        sys.stdout.write("\033[?1049h\033[H\033[2J") # Alternate screen
        sys.stdout.flush()
        tty.setcbreak(fd)

        while running:
            cols, rows = shutil.get_terminal_size((120, 40))
            logs = load_logs()
            
            total_top_btns = 4
            total_items = total_top_btns + len(logs)
            if selected_idx >= total_items and total_items > 0:
                selected_idx = total_items - 1

            MAX_LOGS_VIS = max(5, rows - 10)
            
            # Scrolling logic
            if selected_idx >= total_top_btns:
                log_idx = selected_idx - total_top_btns
                if log_idx < scroll_offset:
                    scroll_offset = log_idx
                elif log_idx >= scroll_offset + MAX_LOGS_VIS:
                    scroll_offset = log_idx - MAX_LOGS_VIS + 1
            else:
                scroll_offset = 0

            buf = ["\033[H"]
            buf.append(f"\033[1m🌐 PEARL FLEET GLOBAL LOGS\033[0m   (Total: {len(logs)})\n")
            buf.append("\n")
            
            # Top buttons
            tb = []
            labels = ["DEL LAST 2", "DEL LAST 5", "DEL LAST 10", "CLEAR ALL"]
            for i, label in enumerate(labels):
                if selected_idx == i:
                    tb.append(f"\033[7m[ {label} ]\033[0m")
                else:
                    tb.append(f"[ {label} ]")
            buf.append("  " + "   ".join(tb) + "\n")
            buf.append("\n")

            # Logs
            vis_logs = logs[scroll_offset:scroll_offset + MAX_LOGS_VIS]
            for i, log in enumerate(vis_logs):
                actual_idx = scroll_offset + i + total_top_btns
                is_sel = (selected_idx == actual_idx)
                
                bg = "\033[48;5;236m" if is_sel else ""
                
                t = log.get("time", "")[-8:] # just HH:MM:SS
                tag = log.get("tag", "")
                tag_color = "\033[92m" if tag == "RENTED" else ("\033[91m" if tag in ("KILLED", "BLACKLISTED") else "\033[93m")
                bot = log.get("bot", "")
                iid = log.get("iid", "")
                mid = log.get("mid", "")
                inst_str = f"{iid} ({mid})" if mid and mid != "?" else iid
                gpu = log.get("gpu", "")
                dph = f"${log.get('dph', 0):.2f}"
                geo = log.get("geo", "")[:4]
                msg = log.get("msg", "")[:30] # Truncate message to keep on one line
                
                c_btn = "\033[7m[ COPY ]\033[27m" if (is_sel and selected_btn == 0) else "[ COPY ]"
                d_btn = "\033[7m[ DEL ]\033[27m" if (is_sel and selected_btn == 1) else "[ DEL ]"
                
                line = f"  {bg}{t:<8} | {tag_color}{tag:<11}\033[0m{bg} | {bot:<9} | {inst_str:<17} | {gpu:<14} | {dph:<6} | {geo:<4} | {msg:<22} {c_btn} {d_btn}\033[0m"
                buf.append(line + "\n")

            buf.append("────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────\n")
            buf.append("  \033[90m[UP/DOWN] Navigate   [LEFT/RIGHT] Select Button   [ENTER] Execute   [r] Reload   [q] Quit\033[0m\n")
            
            buf.append("\033[J")
            out_str = "".join(buf)
            out_str = out_str.replace("\n", "\033[K\n") + "\033[K\033[J"
            sys.stdout.write(out_str)
            sys.stdout.flush()

            # Input handling with timeout for real-time refresh
            r, _, _ = select.select([sys.stdin], [], [], 1.0)
            if not r: continue
            
            ch = os.read(fd, 1).decode('utf-8', 'replace')
            if ch in ('q', 'Q', '\x03'):
                running = False
            elif ch in ('r', 'R', 'к', 'К'):
                running = False
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                sys.stdout.write("\033[?1049l\033[?25h")
                sys.stdout.flush()
                os.execv(sys.executable, [sys.executable] + sys.argv)
            elif ch == '\x1b':
                r2, _, _ = select.select([sys.stdin], [], [], 0.05)
                if r2:
                    ch2 = os.read(fd, 1).decode('utf-8', 'replace')
                    if ch2 in ('[', 'O'):
                        r3, _, _ = select.select([sys.stdin], [], [], 0.05)
                        if r3:
                            ch3 = os.read(fd, 1).decode('utf-8', 'replace')
                            if ch3 == 'A': # UP
                                selected_idx = max(0, selected_idx - 1)
                                selected_btn = 0
                            elif ch3 == 'B': # DOWN
                                selected_idx = min(total_items - 1, selected_idx + 1)
                                selected_btn = 0
                            elif ch3 == 'D': # LEFT
                                if selected_idx >= total_top_btns:
                                    selected_btn = max(0, selected_btn - 1)
                            elif ch3 == 'C': # RIGHT
                                if selected_idx >= total_top_btns:
                                    selected_btn = min(1, selected_btn + 1)
            elif ch in ('\r', '\n'):
                if selected_idx == 0: # DEL LAST 2
                    save_logs(logs[:-2])
                elif selected_idx == 1: # DEL LAST 5
                    save_logs(logs[:-5])
                elif selected_idx == 2: # DEL LAST 10
                    save_logs(logs[:-10])
                elif selected_idx == 3: # CLEAR ALL
                    save_logs([])
                elif selected_idx >= total_top_btns:
                    log_idx = selected_idx - total_top_btns
                    if log_idx < len(logs):
                        target_log = logs[log_idx]
                        if selected_btn == 0: # COPY
                            copy_text = f"[{target_log.get('time')}] {target_log.get('tag')} {target_log.get('iid')} ({target_log.get('gpu')}) - {target_log.get('msg')}"
                            subprocess.run(["pbcopy"], universal_newlines=True, input=copy_text)
                        elif selected_btn == 1: # DELETE
                            logs.pop(log_idx)
                            save_logs(logs)

    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        sys.stdout.write("\033[?1049l\033[?25h")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
