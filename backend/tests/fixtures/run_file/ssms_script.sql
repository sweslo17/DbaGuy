/****** Object:  Table [dbo].[zz_06_tool_tbl]    Script Date: 10/20/2026 12:00:00 PM ******/
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO
SET ANSI_PADDING ON
GO
CREATE TABLE [dbo].[zz_06_tool_tbl](
	[id] [int] IDENTITY(1,1) NOT NULL,
	[label] [nvarchar](50) NOT NULL,
	[data] [varbinary](8) NULL,
 CONSTRAINT [PK_zz_06_tool_tbl] PRIMARY KEY CLUSTERED 
(
	[id] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF, IGNORE_DUP_KEY = OFF, ALLOW_ROW_LOCKS = ON, ALLOW_PAGE_LOCKS = ON, OPTIMIZE_FOR_SEQUENTIAL_KEY = OFF) ON [PRIMARY]
) ON [PRIMARY]
GO
SET IDENTITY_INSERT [dbo].[zz_06_tool_tbl] ON 
GO
INSERT [dbo].[zz_06_tool_tbl] ([id], [label], [data]) VALUES (1, N'it''s', 0x00FF)
GO
INSERT [dbo].[zz_06_tool_tbl] ([id], [label], [data]) VALUES (2, N'中文😀', NULL)
GO
SET IDENTITY_INSERT [dbo].[zz_06_tool_tbl] OFF
GO
/****** Object:  Trigger [dbo].[zz_06_tool_tbl_ai] ******/
CREATE TRIGGER [dbo].[zz_06_tool_tbl_ai] ON [dbo].[zz_06_tool_tbl] AFTER INSERT AS
BEGIN
	SET NOCOUNT ON;
END
GO
ALTER TABLE [dbo].[zz_06_tool_tbl] ENABLE TRIGGER [zz_06_tool_tbl_ai]
GO
