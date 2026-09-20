$ErrorActionPreference='Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class RbLogon {
  [DllImport("advapi32.dll", SetLastError=true)]
  public static extern bool LogonUser(string user, string domain, string password,
                                     int type, int provider, out IntPtr token);
  [DllImport("advapi32.dll", SetLastError=true)]
  public static extern bool ImpersonateLoggedOnUser(IntPtr token);
}
'@
$plain=[Environment]::GetEnvironmentVariable('%(pwenv)s','Process')
$tok=[IntPtr]::Zero
# LOGON32_LOGON_BATCH(4), LOGON32_PROVIDER_DEFAULT(0). A batch logon needs no window station and no
# scheduler -- it only needs the SeBatchLogonRight the principal is granted at creation.
if (-not [RbLogon]::LogonUser('%(user)s','.',$plain,4,0,[ref]$tok)) {
  Write-Error ("LogonUser failed: " + [Runtime.InteropServices.Marshal]::GetLastWin32Error())
  exit 240
}
if (-not [RbLogon]::ImpersonateLoggedOnUser($tok)) {
  Write-Error ("Impersonate failed: " + [Runtime.InteropServices.Marshal]::GetLastWin32Error())
  exit 241
}
# The body runs IMPERSONATED in this process and exits it, so the token dies with the process and
# needs no explicit RevertToSelf.
