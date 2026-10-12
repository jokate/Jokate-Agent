using UnrealBuildTool;
using System.Collections.Generic;

public class __NAME__EditorTarget : TargetRules
{
	public __NAME__EditorTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Editor;
		DefaultBuildSettings = BuildSettingsVersion.Latest;
		IncludeOrderVersion = EngineIncludeOrderVersion.Latest;
		ExtraModuleNames.Add("__NAME__");
	}
}
