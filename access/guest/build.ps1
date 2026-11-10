# Build an Access database (.accdb) from a package made by inkwell.accessbuild: tables, fields
# and their properties, indexes, rows, relationships, then the audited queries, forms, reports,
# macros and modules (LoadFromText), compile, database properties, compact. Run in the user's
# desktop session (vm-ps.sh --user). Writes $Out and $Out.json (counts, per-object errors), or
# $Out.failed with the reason.
param(
    [Parameter(Mandatory = $true)][string]$Package,  # folder with build.json and objects\
    [Parameter(Mandatory = $true)][string]$Out       # the .accdb to write
)
$ErrorActionPreference = 'Stop'
Remove-Item -Force -ErrorAction SilentlyContinue $Out, "$Out.json", "$Out.failed"
$build = Get-Content (Join-Path $Package 'build.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$draft = "$Out.draft.accdb"
Remove-Item -Force -ErrorAction SilentlyContinue $draft

# DAO property types by name; values arrive as text. Unlisted properties are skipped (reported).
$propTypes = @{
    Caption = 10; Description = 10; Format = 10; InputMask = 10; OrderBy = 10; DefaultValue = 10
    DisplayControl = 3; ColumnWidth = 3; ColumnOrder = 3; ShowDatePicker = 3; DefaultView = 3; Orientation = 3
    DecimalPlaces = 2; TextAlign = 2; IMEMode = 2; IMESentenceMode = 2
    ColumnHidden = 1; UnicodeCompression = 1; AppendOnly = 1; OrderByOn = 1; OrderByOnLoad = 1; FilterOnLoad = 1
    AppTitle = 10; StartupForm = 10; StartUpShowDBWindow = 1; StartUpShowStatusBar = 1; AllowFullMenus = 1
    AllowShortcutMenus = 1; AllowBuiltInToolbars = 1; AllowToolbarChanges = 1; AllowBreakIntoCode = 1
    AllowSpecialKeys = 1; 'Perform Name AutoCorrect' = 4; 'Track Name AutoCorrect Info' = 4
}
$report = [ordered]@{ Tables = [ordered]@{}; SkippedProperties = @(); ObjectErrors = @(); Objects = [ordered]@{} }

function ConvertValue([int]$type, [string]$text) {
    switch ($type) {
        1 { return ($text -eq 'True' -or $text -eq '-1' -or $text -eq 'yes') }
        2 { return [byte]$text }
        3 { return [int16]$text }
        4 { return [int32]$text }
        default { return $text }
    }
}

function SetProperty($obj, [string]$name, [string]$value, [string]$where) {
    if (-not $propTypes.ContainsKey($name)) { $script:report.SkippedProperties += "$where.$name"; return }
    $type = $propTypes[$name]
    $v = ConvertValue $type $value
    try { $obj.Properties.Item($name).Value = $v; return } catch {}
    try { $obj.Properties.Append($obj.CreateProperty($name, $type, $v)) }
    catch { $script:report.SkippedProperties += "$where.$name ($($_.Exception.Message))" }
}

$acc = New-Object -ComObject Access.Application
try {
    $dbe = $acc.DBEngine
    $db = $dbe.CreateDatabase($draft, ';LANGID=0x0409;CP=1252;COUNTRY=0', 128)  # 128: .accdb

    foreach ($t in $build.Tables) {
        $td = $db.CreateTableDef($t.Name)
        foreach ($f in $t.Fields) {
            $fd = $td.CreateField($f.Name, [int]$f.Type, [int]$f.Size)
            if (([int]$f.Attributes -band 16) -ne 0) { $fd.Attributes = [int]$f.Attributes }  # AutoNumber
            $fd.Required = [bool]$f.Required
            if ([int]$f.Type -in 10, 12) { $fd.AllowZeroLength = [bool]$f.AllowZeroLength }
            if ($f.DefaultValue) { $fd.DefaultValue = $f.DefaultValue }
            if ($f.ValidationRule) { $fd.ValidationRule = $f.ValidationRule; $fd.ValidationText = $f.ValidationText }
            $td.Fields.Append($fd)
        }
        foreach ($ix in $t.Indexes) {
            if ($ix.Foreign) { continue }  # made by the relationships
            $i = $td.CreateIndex($ix.Name)
            foreach ($xf in $ix.Fields) { $c = $i.CreateField($xf.Name); $c.Attributes = [int]$xf.Attributes; $i.Fields.Append($c) }
            $i.Primary = [bool]$ix.Primary; $i.Unique = [bool]$ix.Unique
            $i.IgnoreNulls = [bool]$ix.IgnoreNulls; $i.Required = [bool]$ix.Required
            $td.Indexes.Append($i)
        }
        $db.TableDefs.Append($td)
        $td = $db.TableDefs.Item($t.Name)
        foreach ($f in $t.Fields) {
            foreach ($p in $f.Properties.PSObject.Properties) { SetProperty $td.Fields.Item($f.Name) $p.Name $p.Value "$($t.Name).$($f.Name)" }
        }
        foreach ($p in $t.Properties.PSObject.Properties) { SetProperty $td $p.Name $p.Value $t.Name }

        # Rows, as SQL INSERTs (no transaction: through Access, BeginTrans and the database's
        # workspace did not match). (Recordset.Fields(...).Value failed in
        # long loops with "Object invalid or no longer set".) dbFailOnError: any row error stops.
        $types = @($t.Fields | ForEach-Object { [int]$_.Type })
        $columns = ($t.Fields | ForEach-Object { '[' + $_.Name + ']' }) -join ', '
        foreach ($row in $t.Rows) {
            $values = for ($k = 0; $k -lt $types.Count; $k++) {
                $v = $row[$k]
                if ($null -eq $v) { 'NULL' }
                elseif ($types[$k] -eq 8) {
                    '#' + [datetime]::Parse([string]$v, [Globalization.CultureInfo]::InvariantCulture).ToString('yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture) + '#'
                }
                elseif ($types[$k] -in 3, 4) {
                    if ([string]$v -notmatch '^-?\d+$') { throw "$($t.Name).$($t.Fields[$k].Name): not an integer" }
                    [string]$v
                }
                else { "'" + ([string]$v).Replace("'", "''") + "'" }
            }
            $db.Execute("INSERT INTO [$($t.Name)] ($columns) VALUES ($($values -join ', '))", 128)
        }
        $n = $db.OpenRecordset("SELECT COUNT(*) FROM [$($t.Name)]").Fields.Item(0).Value
        if ($n -ne @($t.Rows).Count) { throw "$($t.Name): wrote $n rows, expected $(@($t.Rows).Count)" }
        $report.Tables[$t.Name] = $n
    }

    foreach ($r in $build.Relations) {
        $rel = $db.CreateRelation($r.Name, $r.Table, $r.ForeignTable, [int]$r.Attributes)
        foreach ($rf in $r.Fields) { $x = $rel.CreateField($rf.Name); $x.ForeignName = $rf.ForeignName; $rel.Fields.Append($x) }
        $db.Relations.Append($rel)
    }
    $report.Relations = $db.Relations.Count
    $db.Close()

    # Objects, through Access. The startup form is set only after everything is in place.
    $acc.OpenCurrentDatabase($draft)
    # LoadFromText writes its reasons to errors*.txt in the current folder (at most ~100 files).
    $errDir = Join-Path $env:TEMP ("autowin-import-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $errDir | Out-Null
    Push-Location $errDir
    [IO.Directory]::SetCurrentDirectory($errDir)
    $kindType = @{ queries = 1; forms = 2; reports = 3; macros = 4; modules = 5 }
    $order = @{ queries = 0; modules = 1; forms = 2; reports = 3; macros = 4 }
    $objects = @($build.Objects) | Sort-Object { $order[$_.Kind] }, { if ($_.Name -match 'sub(form|report)') { 0 } else { 1 } }, Name
    foreach ($o in $objects) {
        $file = Join-Path $Package ($o.File -replace '/', '\')
        try {
            $acc.LoadFromText($kindType[$o.Kind], $o.Name, $file)
            $report.Objects[$o.Kind] = 1 + [int]$report.Objects[$o.Kind]
        } catch {
            $report.ObjectErrors += "$($o.Kind)/$($o.Name): $($_.Exception.Message)"
        }
    }
    Pop-Location
    $report.ImportErrorDetails = @(Get-ChildItem $errDir -Filter 'errors*.txt' | ForEach-Object { (Get-Content $_.FullName | Where-Object { $_ -match '\S' }) -join ' | ' })
    [IO.Directory]::SetCurrentDirectory($env:TEMP)
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $errDir
    $report.References = @()
    foreach ($r in @($build.References)) {
        try { [void]$acc.References.AddFromGuid($r.Guid, [int]$r.Major, [int]$r.Minor); $report.References += $r.Name }
        catch { $report.ObjectErrors += "reference $($r.Name): $($_.Exception.Message)" }
    }
    try { $acc.RunCommand(126) } catch { $report.ObjectErrors += "compile: $($_.Exception.Message)" }  # acCmdCompileAndSaveAllModules
    $report.Compiled = [bool]$acc.IsCompiled
    $cdb = $acc.CurrentDb()
    foreach ($p in $build.DatabaseProperties.PSObject.Properties) {
        if ($propTypes.ContainsKey($p.Name)) { SetProperty $cdb $p.Name $p.Value 'database' }
    }
    # CurrentDb() keeps the file open ("already opened by user Admin"): release it before compacting,
    # and if the lock remains, compact with a fresh Access instance.
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($cdb); $cdb = $null
    $acc.CloseCurrentDatabase()
    [GC]::Collect(); [GC]::WaitForPendingFinalizers()
    try { $dbe.CompactDatabase($draft, $Out) }
    catch {
        $acc.Quit(); [void][Runtime.InteropServices.Marshal]::ReleaseComObject($acc)
        [GC]::Collect(); [GC]::WaitForPendingFinalizers(); Start-Sleep -Seconds 3
        $acc = New-Object -ComObject Access.Application
        $acc.DBEngine.CompactDatabase($draft, $Out)
    }
    Remove-Item -Force $draft
    $report.Bytes = (Get-Item $Out).Length
    $report | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -Path "$Out.json"
    "tables={0} relations={1} objects={2} errors={3} compiled={4} bytes={5}" -f $report.Tables.Count, $report.Relations,
        (($report.Objects.Values | Measure-Object -Sum).Sum), $report.ObjectErrors.Count, $report.Compiled, $report.Bytes
}
catch {
    $where = "line $($_.InvocationInfo.ScriptLineNumber): $($_.InvocationInfo.Line.Trim())"
    Set-Content -Path "$Out.failed" -Value "$_ ($where)"
    throw "$_ ($where)"
}
finally {
    try { $acc.CloseCurrentDatabase() } catch {}
    try { $acc.Quit() } catch {}
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($acc)
}
