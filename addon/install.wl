(* install.wl - Install MathematicaMCP for auto-loading *)
(* Run this once: wolframscript -file install.wl *)
(* Safe to re-run: any previous MathematicaMCP section in init.m is replaced, *)
(* so upgrades re-point the loader at THIS package copy (a stale loader line *)
(* pointing at an old uv/pip cache path was the main cause of protocol-skew *)
(* warnings after upgrading the Python package). *)

$MCPPackagePath = DirectoryName[$InputFileName];
If[$MCPPackagePath === "", $MCPPackagePath = Directory[]];

$MCPPackageFile = FileNameJoin[{$MCPPackagePath, "MathematicaMCP.wl"}];
$InitPath = FileNameJoin[{$UserBaseDirectory, "Kernel", "init.m"}];

If[!FileExistsQ[$MCPPackageFile],
  Print["Error: MathematicaMCP.wl not found at: ", $MCPPackageFile];
  Exit[1]
];

initContent = If[FileExistsQ[$InitPath], Import[$InitPath, "Text"], ""];
If[!StringQ[initContent], initContent = ""];

$MCPPackageFileNormalized = StringReplace[$MCPPackageFile, "\\" -> "/"];

(* Explicit markers delimit the managed section. The previous installer removed
   every line containing "MathematicaMCP", which cannot express a multi-line
   block: the guard below has lines that do not mention the package, and a
   line-wise filter would strip the body and leave a dangling If[...] that
   breaks every kernel launch. *)
$MCPBegin = "(* MathematicaMCP:begin - managed by install.wl, do not edit *)";
$MCPEnd = "(* MathematicaMCP:end *)";

loadCode = StringJoin[
  "\n\n", $MCPBegin, "\n",
  "(* Skipped in kernels spawned by the Python MCP server itself. Such a kernel\n",
  "   must never host the addon socket - the server would connect to its own\n",
  "   front-end-less child instead of this session, and the real Mathematica\n",
  "   could no longer bind the port. Note that -noinit does NOT suppress this\n",
  "   file under wolframscript, so the check has to live here. *)\n",
  "If[Environment[\"MATHEMATICA_MCP_CHILD\"] =!= \"1\",\n",
  "  Quiet @ Get[\"", $MCPPackageFileNormalized, "\"];\n",
  "  MathematicaMCP`StartMCPServer[];\n",
  "];\n",
  $MCPEnd, "\n"
];

(* Idempotent rewrite: drop the marked section if present, else fall back to the
   legacy line-wise filter so an install.m written by an older version upgrades
   cleanly instead of accumulating a second loader. *)
wasConfigured = StringContainsQ[initContent, "MathematicaMCP"];
cleanedContent =
  If[StringContainsQ[initContent, $MCPBegin] && StringContainsQ[initContent, $MCPEnd],
    StringReplace[
      initContent,
      Shortest[$MCPBegin ~~ ___ ~~ $MCPEnd] -> ""
    ],
    StringRiffle[
      Select[StringSplit[initContent, "\n"], !StringContainsQ[#, "MathematicaMCP"] &],
      "\n"
    ]
  ];
cleanedContent = StringTrim[cleanedContent, RegularExpression["[\\n\\s]+$"]];

If[!DirectoryQ[DirectoryName[$InitPath]],
  CreateDirectory[DirectoryName[$InitPath]]
];

Export[$InitPath, cleanedContent <> loadCode, "Text"];

If[wasConfigured,
  Print["=== MathematicaMCP Updated ==="];
  Print[""];
  Print["Replaced the existing MathematicaMCP section in: ", $InitPath];
  Print["The loader now points at: ", $MCPPackageFileNormalized];
  ,
  Print["=== MathematicaMCP Installation Complete ==="];
  Print[""];
  Print["Added to: ", $InitPath];
];
Print[""];
Print["The MCP server will now auto-start when Mathematica launches."];
Print[""];
Print["To start manually in a running session:"];
Print["  Get[\"", $MCPPackageFileNormalized, "\"];"];
Print["  StartMCPServer[]"];
Print[""];
Print["To uninstall, remove the MathematicaMCP section from init.m"];
