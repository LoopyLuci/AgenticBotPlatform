# R8 rules for BotServer's mobile app. Retrofit, OkHttp, Hilt, and
# kotlinx-serialization all ship their own consumer-proguard-rules bundled
# in their AARs, so this file starts minimal on purpose: flip
# isMinifyEnabled on, build a release APK, smoke-test every screen, and
# add explicit -keep rules here only for whatever actually breaks, rather
# than pre-writing a speculative ruleset for reflection paths this app
# doesn't necessarily hit.

# kotlinx.serialization needs the serializer companion objects and the
# @Serializable classes' fields kept, since serialization is driven by
# reflection over generated Companion.serializer() methods.
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.AnnotationsKt
-keepclassmembers class com.botserver.mobile.data.dto.** {
    *** Companion;
}
-keepclasseswithmembers class com.botserver.mobile.data.dto.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class com.botserver.mobile.data.dto.**$$serializer { *; }
