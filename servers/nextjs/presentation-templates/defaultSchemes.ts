import * as z from "zod";

export const DEFAULT_ICON_URL = "/static/icons/placeholder.svg";
export const DEFAULT_ICON_QUERY = "placeholder icon";

export const ImageSchema = z.object({
    __image_url__: z.url().meta({
        description: "URL to image",
    }),
    __image_prompt__: z.string().meta({
        description: "Prompt used to generate the image",
    }).min(10).max(50),
})

export const IconSchema = z.object({
    __icon_url__: z.string().meta({
        description: "URL to icon",
    }),
    __icon_query__: z.string().meta({
        description: "Query used to search the icon",
    }).min(5).max(20),
})

type IconLike = {
    __icon_url__?: unknown;
    __icon_query__?: unknown;
} | null | undefined;

export function getIconUrl(icon: IconLike): string {
    return typeof icon?.__icon_url__ === "string" && icon.__icon_url__.trim()
        ? icon.__icon_url__
        : DEFAULT_ICON_URL;
}

export function getIconQuery(icon: IconLike): string {
    return typeof icon?.__icon_query__ === "string" && icon.__icon_query__.trim()
        ? icon.__icon_query__
        : DEFAULT_ICON_QUERY;
}
