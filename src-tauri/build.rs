fn main() {
    // The release label baked into the About panel (src/menu.rs): CI sets it from the tag
    println!("cargo:rerun-if-env-changed=DBAGUY_RELEASE");
    tauri_build::build()
}
