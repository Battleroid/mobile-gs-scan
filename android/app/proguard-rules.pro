# Keep ARCore native bindings (they're loaded reflectively).
-keep class com.google.ar.** { *; }
-keep class com.google.ar.core.** { *; }

# kotlinx.serialization
-keepattributes *Annotation*, InnerClasses
-keepclasseswithmembers class * {
    kotlinx.serialization.KSerializer serializer(...);
}
