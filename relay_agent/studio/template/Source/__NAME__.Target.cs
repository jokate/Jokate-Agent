using UnrealBuildTool;
using System.Collections.Generic;

public class __NAME__Target : TargetRules
{
	public __NAME__Target(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Game;
		DefaultBuildSettings = BuildSettingsVersion.Latest;
		IncludeOrderVersion = EngineIncludeOrderVersion.Latest;
		ExtraModuleNames.Add("__NAME__");
	}
}
