plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// 【2026-11-14】版本号从 CI 注入（tag v0.3.0 → 0.3.0），本地不传就用默认值。
// 之前 versionCode 一直是 1：发第二个包时手机可能把新包当旧包，覆盖安装会失败；
// 用户也没法从「设置 → 应用信息」分清装的是哪一版。
// versionCode 用 major*10000 + minor*100 + patch，单调递增。
val versionFromCi = (System.getenv("APP_VERSION") ?: "").trim().removePrefix("v")
val appVersionName = if (versionFromCi.isEmpty()) "0.3.0" else versionFromCi
val versionParts = appVersionName.split(".").map { part ->
    part.filter { ch -> ch in '0'..'9' }.toIntOrNull()
}
val major = versionParts.getOrNull(0)
val minor = versionParts.getOrNull(1)
val patch = versionParts.getOrNull(2)
// 注意：非 tag 构建传进来的是 0.0.0-dev，算出来是 0 —— 而 versionCode 必须 > 0，
// 否则 Gradle 直接报错。所以这里必须兜一个下限 1。
val appVersionCode = if (major != null && minor != null && patch != null) {
    (major * 10000 + minor * 100 + patch).coerceAtLeast(1)
} else {
    1
}

android {
    namespace = "com.jobpilot.mobile"
    compileSdk = 34

    // 【2026-11-15】签名密钥从 CI 环境变量读 —— 仓库里不放私钥。
    //
    // 背景：以前 CI 每次新建 debug keystore → 每次签名都不同 → 无法覆盖安装 → 只能卸载 →
    // SharedPreferences（今日计数 / 已打记录）被清空。签名固定后这些问题一起消失。
    //
    // 三个变量由 workflow 注入：
    //   KEYSTORE_FILE   .p12 的绝对路径（CI 从 secret KEYSTORE_B64 解码到 $RUNNER_TEMP）
    //   KEYSTORE_PASS   密钥口令（secret KEYSTORE_PASS）
    //   KEYSTORE_ALIAS  别名，默认 jobpilot
    //
    // 本地没有 secrets 时回退到 "testkey.p12"（该文件**不入库**）——
    // 想在本地出包，得自己先 `openssl pkcs12 -export ... -out android/app/testkey.p12`。
    signingConfigs {
        create("stable") {
            val ksFile = System.getenv("KEYSTORE_FILE").orEmpty().ifBlank { "testkey.p12" }
            val ksPass = System.getenv("KEYSTORE_PASS").orEmpty().ifBlank { "jobpilot-test-2026" }
            val ksAlias = System.getenv("KEYSTORE_ALIAS").orEmpty().ifBlank { "jobpilot" }
            storeFile = file(ksFile)
            storeType = "PKCS12"
            storePassword = ksPass
            keyAlias = ksAlias
            keyPassword = ksPass
        }
    }

    defaultConfig {
        applicationId = "com.jobpilot.mobile"
        minSdk = 26
        targetSdk = 34
        versionCode = appVersionCode
        versionName = appVersionName
    }

    buildTypes {
        // debug 也签这把：CI 出的就是 debug 包，但签名必须和下次一致，才能覆盖安装。
        getByName("debug") {
            signingConfig = signingConfigs.getByName("stable")
        }
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("stable")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

// 刻意零依赖：不引 androidx、不引 Compose。
// 理由：v0.1 只需要一个无障碍服务 + 一个调试页面，
// 每多一个依赖就多一个 CI 首次构建失败的可能。等 D2 真正画界面时再引入。
