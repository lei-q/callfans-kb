// callfans 桌面壳：拉起 sidecar（callfans serve）+ 加载 Web 控制台
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::Manager;

struct Sidecar(Mutex<Option<Child>>);

fn spawn_sidecar(resource_dir: &std::path::Path) -> Option<Child> {
    // sidecar 名：Windows 为 callfans.exe，其他平台为 callfans
    let name = if cfg!(target_os = "windows") { "callfans.exe" } else { "callfans" };
    let exe = resource_dir.join(name);
    Command::new(&exe)
        .args(["serve", "--no-browser", "--port", "8765"])
        .spawn()
        .ok()
}

fn main() {
    let app = tauri::Builder::default()
        .manage(Sidecar(Mutex::new(None)))
        .setup(|app| {
            let dir = app.path().resource_dir()?;
            let child = spawn_sidecar(&dir).expect("无法启动 callfans sidecar");
            *app.state::<Sidecar>().0.lock().unwrap() = Some(child);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    // 窗口关闭 → 停 sidecar → 退出
    let handle = app.handle().clone();
    app.run(move |_app_handle, event| {
        if let tauri::RunEvent::ExitRequested { .. } = event {
            if let Some(mut c) = handle.state::<Sidecar>().0.lock().unwrap().take() {
                let _ = c.kill();
            }
        }
    });
}
