import org.scalajs.linker.interface.ModuleKind
import org.scalajs.jsenv.nodejs.NodeJSEnv

lazy val seeta_ai_assistant_app = project
  .in(file(""))
  .enablePlugins(org.scalajs.sbtplugin.ScalaJSPlugin, golem.sbt.GolemPlugin)
  .settings(
    name                            := "seeta-ai-assistant",
    scalaJSUseMainModuleInitializer := false,
    scalacOptions += "-experimental",
    Compile / scalaJSLinkerConfig ~= (_.withModuleKind(ModuleKind.ESModule)),
    libraryDependencies ++= Seq(
      "cloud.golem"       %%% "golem-scala-core"   % "1.5.1",
      "cloud.golem"       %%% "golem-scala-model"  % "1.5.1",
      "cloud.golem"        %% "golem-scala-macros" % "1.5.1",
      "io.github.cquiroz" %%% "scala-java-time"    % "2.6.0",
      "org.scalameta"     %%% "munit"              % "1.0.3" % Test
    ),
    testFrameworks += new TestFramework("munit.Framework"),
    jsEnv := new NodeJSEnv(
      NodeJSEnv
        .Config()
        .withArgs(
          List(
            "--experimental-loader",
            s"file://${baseDirectory.value.getAbsolutePath}/golem-stub-loader.mjs",
            "--experimental-vm-modules"
          )
        )
    ),
    golem.sbt.GolemPlugin.autoImport.golemBasePackage := Some("seeta_ai_assistant")
  )
