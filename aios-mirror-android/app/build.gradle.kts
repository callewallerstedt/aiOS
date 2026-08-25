plugins {
    id("com.android.application")
}

android {
    namespace = "com.aios.mirror"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.aios.mirror"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
}
