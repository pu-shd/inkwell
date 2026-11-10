# Export an Access database's structure and objects for an audited rebuild (run in the user's
# desktop session, e.g. `vm-ps.sh --user tester`). Works on a copy; the original is never opened.
#
#   1. DAO opens the copy (no startup code runs), records and removes StartupForm.
#   2. schema.json: tables (fields with properties, table properties incl. saved filters),
#      indexes, relationships, queries, database properties.
#   3. Access opens the copy and SaveAsText exports queries, forms, reports, macros, modules.
#   4. Everything is zipped to $Out.zip; $Out.done lists counts, or $Out.failed holds the reason.
#
# The export can hold personal data (saved filters, form defaults, VBA): it is audited on the host
# before anything from it reaches a rebuilt database.
param(
    [Parameter(Mandatory = $true)][string]$Mdb,
    [Parameter(Mandatory = $true)][string]$Out
)
$ErrorActionPreference = 'Stop'
Remove-Item -Force -ErrorAction SilentlyContinue "$Out.zip", "$Out.done", "$Out.failed"
$work = "$Out.work"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $work
New-Item -ItemType Directory -Force -Path "$work\objects" | Out-Null
$copy = Join-Path $work ('source' + [IO.Path]::GetExtension($Mdb))
Copy-Item $Mdb $copy

function Props($obj, [string[]]$skip) {
    $h = [ordered]@{}
    foreach ($p in $obj.Properties) {
        if ($skip -contains $p.Name) { continue }
        try { $v = $p.Value } catch { continue }
        if ($null -eq $v -or $v -is [System.__ComObject]) { continue }
        $h[$p.Name] = "$v"
    }
    $h
}

$acc = New-Object -ComObject Access.Application
try {
    # 1-2. Structure through DAO.
    $db = $acc.DBEngine.OpenDatabase($copy)
    $dbProps = Props $db @('Name', 'Connect', 'Transactions', 'Updatable', 'CollatingOrder', 'QueryTimeout',
                           'Version', 'RecordsAffected', 'ReplicaID', 'DesignMasterID', 'Connection')
    if ($db.Properties | Where-Object Name -eq 'StartupForm') { $db.Properties.Delete('StartupForm') }

    $tables = foreach ($td in $db.TableDefs) {
        if ($td.Name -like 'MSys*' -or $td.Name -like '~*') { continue }
        $fields = foreach ($f in $td.Fields) {
            [ordered]@{
                Name = $f.Name; Type = $f.Type; Size = $f.Size; Attributes = $f.Attributes
                OrdinalPosition = $f.OrdinalPosition; Required = $f.Required
                AllowZeroLength = $(try { $f.AllowZeroLength } catch { $null })
                DefaultValue = $f.DefaultValue; ValidationRule = $f.ValidationRule; ValidationText = $f.ValidationText
                Properties = Props $f @('Name', 'Type', 'Size', 'Attributes', 'OrdinalPosition', 'Required',
                    'AllowZeroLength', 'DefaultValue', 'ValidationRule', 'ValidationText', 'Value', 'ValidateOnSet',
                    'CollatingOrder', 'DataUpdatable', 'ForeignName', 'SourceField', 'SourceTable', 'FieldSize',
                    'OriginalValue', 'VisibleValue', 'GUID')
            }
        }
        $indexes = foreach ($ix in $td.Indexes) {
            [ordered]@{
                Name = $ix.Name; Primary = $ix.Primary; Unique = $ix.Unique; IgnoreNulls = $ix.IgnoreNulls
                Required = $ix.Required; Foreign = $ix.Foreign
                Fields = @(foreach ($xf in $ix.Fields) { [ordered]@{ Name = $xf.Name; Attributes = $xf.Attributes } })
            }
        }
        [ordered]@{
            Name = $td.Name; Attributes = $td.Attributes; ValidationRule = $td.ValidationRule
            ValidationText = $td.ValidationText
            Properties = Props $td @('Name', 'Attributes', 'ValidationRule', 'ValidationText', 'Updatable',
                'DateCreated', 'LastUpdated', 'RecordCount', 'Connect', 'SourceTableName', 'ConflictTable',
                'ReplicaFilter', 'GUID', 'NameMap')
            Fields = @($fields); Indexes = @($indexes)
        }
    }
    $relations = foreach ($r in $db.Relations) {
        [ordered]@{
            Name = $r.Name; Table = $r.Table; ForeignTable = $r.ForeignTable; Attributes = $r.Attributes
            Fields = @(foreach ($rf in $r.Fields) { [ordered]@{ Name = $rf.Name; ForeignName = $rf.ForeignName } })
        }
    }
    $queries = foreach ($q in $db.QueryDefs) {
        [ordered]@{ Name = $q.Name; Type = $q.Type; SQL = $q.SQL; Hidden = ($q.Name -like '~*') }
    }
    $db.Close()
    $schema = [ordered]@{
        Source = [IO.Path]::GetFileName($Mdb); DatabaseProperties = $dbProps
        Tables = @($tables); Relations = @($relations); Queries = @($queries)
    }
    $schema | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -Path "$work\schema.json"

    # 3. Objects as text, through Access (the startup form is gone from the copy).
    $acc.OpenCurrentDatabase($copy)
    $kinds = @(
        @{ Type = 1; Dir = 'queries'; Items = @($acc.CurrentData.AllQueries) },
        @{ Type = 2; Dir = 'forms'; Items = @($acc.CurrentProject.AllForms) },
        @{ Type = 3; Dir = 'reports'; Items = @($acc.CurrentProject.AllReports) },
        @{ Type = 4; Dir = 'macros'; Items = @($acc.CurrentProject.AllMacros) },
        @{ Type = 5; Dir = 'modules'; Items = @($acc.CurrentProject.AllModules) }
    )
    $counts = [ordered]@{}
    $manifest = foreach ($k in $kinds) {
        New-Item -ItemType Directory -Force -Path "$work\objects\$($k.Dir)" | Out-Null
        $n = 0
        foreach ($item in $k.Items) {
            if ($item.Name -like '~*') { continue }
            $n++
            $file = '{0:D3}.txt' -f $n  # names can hold characters that files cannot
            $acc.SaveAsText($k.Type, $item.Name, "$work\objects\$($k.Dir)\$file")
            [ordered]@{ Kind = $k.Dir; Name = $item.Name; File = "objects/$($k.Dir)/$file" }
        }
        $counts[$k.Dir] = $n
    }
    # VBA references by name, GUID and version only (paths can hold user names).
    $references = foreach ($r in $acc.References) {
        [ordered]@{ Name = $r.Name; Guid = $r.Guid; Major = $r.Major; Minor = $r.Minor; BuiltIn = $r.BuiltIn; IsBroken = $r.IsBroken }
    }
    $schema.References = @($references)
    $schema | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 -Path "$work\schema.json"
    $acc.CloseCurrentDatabase()
    @($manifest) | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -Path "$work\manifest.json"
    Remove-Item -Force $copy

    # 4. One file for the (slow) guest agent.
    Compress-Archive -Path "$work\*" -DestinationPath "$Out.zip" -CompressionLevel Optimal
    $summary = @((Get-Date -Format o), "tables=$(@($tables).Count) relations=$(@($relations).Count) queries=$(@($queries).Count)") +
        @($counts.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" })
    Set-Content -Path "$Out.done" -Value $summary
    $summary -join '; '
}
catch {
    Set-Content -Path "$Out.failed" -Value "$_"
    throw
}
finally {
    try { $acc.Quit() } catch {}
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($acc)
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $work
}
